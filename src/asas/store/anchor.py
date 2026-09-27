"""External, signed anchors for the hash-chained audit log.

The audit chain proves the log is internally consistent. It cannot, on its own, stop someone
with write access to the database from rewriting an entry and recomputing every later hash.
Anchors close that gap. After each pipeline run (or on demand) the chain head `(seq, hash)`
is signed and written to a location the database account cannot modify:

- a write-once directory (files are created exclusively and made read-only)
- in production, object storage with retention lock (S3 Object Lock, Azure immutable blobs)
  or a separate ledger. Any object with a `write(anchor)` / `anchors()` pair is a sink.

Verification recomputes the chain and checks every anchor: its signature and that the entry
at `seq` still has the anchored hash. A rewritten or truncated history fails even if the chain
itself was rebuilt consistently.

Ed25519 is the default: the signing key can live with the job that anchors, and auditors
verify with the public key alone. HMAC-SHA256 is available where only a shared secret is
possible.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict

from asas.core.config import Config
from asas.core.errors import AsasError
from asas.core.ids import canonical_json
from asas.store.db import Store, ts_key, utcnow


class AnchorError(AsasError):
    """Anchoring is misconfigured or an anchor could not be written."""


class Anchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    seq: int
    hash: str
    at: datetime
    algorithm: str
    key_id: str
    signature: str

    def body(self) -> bytes:
        return canonical_json({"seq": self.seq, "hash": self.hash, "at": ts_key(self.at)}).encode()


class Signer(Protocol):
    algorithm: str
    key_id: str

    def sign(self, data: bytes) -> str: ...


class Verifier(Protocol):
    algorithm: str
    key_id: str

    def verify(self, data: bytes, signature: str) -> bool: ...


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Ed25519Signer:
    algorithm = "ed25519"

    def __init__(self, private_pem: bytes) -> None:
        key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise AnchorError("the audit signing key is not an Ed25519 private key")
        self._key = key
        self.public = Ed25519Verifier.from_key(key.public_key())
        self.key_id = self.public.key_id

    def sign(self, data: bytes) -> str:
        return _b64(self._key.sign(data))


class Ed25519Verifier:
    algorithm = "ed25519"

    def __init__(self, key: Ed25519PublicKey) -> None:
        self._key = key
        raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.key_id = hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def from_key(key: Ed25519PublicKey) -> Ed25519Verifier:
        return Ed25519Verifier(key)

    @staticmethod
    def from_pem(public_pem: bytes) -> Ed25519Verifier:
        key = serialization.load_pem_public_key(public_pem)
        if not isinstance(key, Ed25519PublicKey):
            raise AnchorError("the audit public key is not an Ed25519 public key")
        return Ed25519Verifier(key)

    def verify(self, data: bytes, signature: str) -> bool:
        try:
            self._key.verify(base64.b64decode(signature), data)
        except (InvalidSignature, ValueError):
            return False
        return True


class HmacSigner:
    algorithm = "hmac-sha256"

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise AnchorError("the HMAC audit secret must be at least 32 bytes")
        self._secret = secret
        self.key_id = hashlib.sha256(b"asas-anchor|" + secret).hexdigest()[:16]

    def sign(self, data: bytes) -> str:
        return _b64(hmac.new(self._secret, data, hashlib.sha256).digest())

    def verify(self, data: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(data), signature)


def generate_ed25519_keypair() -> tuple[bytes, bytes]:
    """(private PEM, public PEM) for `asas audit keygen`."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


def _env_bytes(name: str) -> bytes:
    """A key from the environment: the value itself, or a path to a file holding it."""
    value = os.environ.get(name, "")
    if not value:
        raise AnchorError(f"audit key not configured: set ${name}")
    path = Path(value)
    if len(value) < 512 and path.is_file():
        return path.read_bytes()
    return value.encode("utf-8")


def signer_from_config(cfg: Config) -> Signer:
    kind = cfg.string("audit", "signing")
    key = _env_bytes(cfg.string("audit", "key_env"))
    if kind == "ed25519":
        return Ed25519Signer(key)
    if kind == "hmac":
        return HmacSigner(key)
    raise AnchorError(f"unknown audit.signing {kind!r} (ed25519 | hmac)")


def verifier_from_config(cfg: Config) -> Verifier:
    kind = cfg.string("audit", "signing")
    if kind == "ed25519":
        public_env = cfg.string("audit", "public_key_env")
        if os.environ.get(public_env):
            return Ed25519Verifier.from_pem(_env_bytes(public_env))
        return Ed25519Signer(_env_bytes(cfg.string("audit", "key_env"))).public
    if kind == "hmac":
        return HmacSigner(_env_bytes(cfg.string("audit", "key_env")))
    raise AnchorError(f"unknown audit.signing {kind!r} (ed25519 | hmac)")


class FileAnchorSink:
    """Write-once files: created exclusively, then made read-only. Point this at storage the
    database account cannot write (a separate mount, a WORM share, a synced bucket)."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def write(self, anchor: Anchor) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        # one file per chain position: a second, different hash at the same seq is a fork
        path = self.directory / f"anchor-{anchor.seq:012d}.json"
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(anchor.model_dump_json())
        except FileExistsError:
            existing = Anchor.model_validate_json(path.read_text(encoding="utf-8"))
            if existing.hash != anchor.hash:
                raise AnchorError(f"history fork: a different hash is anchored at {path}") from None
            return path
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        return path

    def anchors(self) -> list[Anchor]:
        if not self.directory.is_dir():
            return []
        return [
            Anchor.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self.directory.glob("anchor-*.json"))
        ]


def anchor_audit(store: Store, signer: Signer, sink: FileAnchorSink) -> Anchor | None:
    head = store.audit_head()
    if head is None:
        return None
    seq, digest = head
    unsigned = Anchor(
        seq=seq, hash=digest, at=utcnow(), algorithm=signer.algorithm, key_id=signer.key_id,
        signature="",
    )  # fmt: skip
    anchor = unsigned.model_copy(update={"signature": signer.sign(unsigned.body())})
    sink.write(anchor)
    return anchor


@dataclass
class AnchorReport:
    chain_valid: bool
    entries: int
    anchors: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chain_valid and not self.problems

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "chain_valid": self.chain_valid,
            "entries": self.entries,
            "anchors": self.anchors,
            "problems": self.problems,
        }


def verify_with_anchors(
    store: Store, verifier: Verifier, anchors: Iterable[Anchor]
) -> AnchorReport:
    chain_valid, entries = store.verify_audit_chain()
    anchored = list(anchors)
    report = AnchorReport(chain_valid, entries, len(anchored))
    for a in anchored:
        label = f"anchor seq={a.seq}"
        if a.algorithm != verifier.algorithm or a.key_id != verifier.key_id:
            report.problems.append(f"{label}: signed with another key ({a.key_id})")
            continue
        if not verifier.verify(a.body(), a.signature):
            report.problems.append(f"{label}: signature invalid")
            continue
        current = store.audit_hash_at(a.seq)
        if current is None:
            report.problems.append(f"{label}: entry missing (history truncated)")
        elif current != a.hash:
            report.problems.append(f"{label}: entry hash changed (history rewritten)")
    return report


def anchor_summary(anchors: Iterable[Anchor]) -> dict[str, object]:
    items = list(anchors)
    last = max(items, key=lambda a: a.seq) if items else None
    return {
        "anchors": len(items),
        "last_seq": last.seq if last else None,
        "last_at": last.at.isoformat() if last else None,
    }


def dump(anchor: Anchor) -> str:
    return json.dumps(anchor.model_dump(mode="json"), indent=1)
