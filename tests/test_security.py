"""Authentication, production readiness, HTTP hardening and externally anchored audit."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from asas.api.app import create_app
from asas.core.auth import (
    AuthError,
    EntitlementError,
    Entitlements,
    OidcAuthenticator,
    TrustedProxyAuthenticator,
    build_authenticator,
)
from asas.core.config import Config
from asas.core.ids import canonical_json, content_hash
from asas.core.readiness import NotReady, checks
from asas.core.security import SYSTEM, Role
from asas.services.demo import DemoResult
from asas.store.anchor import (
    AnchorError,
    Ed25519Signer,
    FileAnchorSink,
    HmacSigner,
    anchor_audit,
    generate_ed25519_keypair,
    verify_with_anchors,
)
from asas.store.db import Store

ISSUER = "https://idp.test/realms/surveillance"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC = KEY.public_key()


def _oidc_cfg(cfg: Config) -> Config:
    return (
        cfg.with_value("oidc", "security", "mode")
        .with_value(ISSUER, "security", "oidc", "issuer")
        .with_value(f"{ISSUER}/certs", "security", "oidc", "jwks_url")
    )


def _token(**overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "asas",
        "iat": now,
        "exp": now + 300,
        "preferred_username": "jane.analyst",
        "groups": ["ASAS-ANALYSTS", "ASAS-DESK-EQ"],
    }
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def _oidc(cfg: Config) -> OidcAuthenticator:
    oidc = _oidc_cfg(cfg)
    return OidcAuthenticator(oidc, Entitlements.from_config(oidc), key_resolver=lambda _t: PUBLIC)


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- OIDC


def test_oidc_maps_groups_to_least_privilege_principal(cfg: Config) -> None:
    who = _oidc(cfg).authenticate(_bearer(_token()))
    assert who.user_id == "jane.analyst"
    assert who.roles == frozenset({Role.INVESTIGATOR}) and who.desks == frozenset({"EQ"})
    assert not who.person_data


@pytest.mark.parametrize(
    "token",
    [
        _token(exp=int(time.time()) - 3600),  # expired
        _token(aud="another-app"),
        _token(iss="https://evil.test"),
        _token(exp=None),  # exp is required
        jwt.encode({"preferred_username": "x", "groups": ["ASAS-ADMINS"]}, None, algorithm="none"),
        jwt.encode(  # key confusion: HMAC "signed" with the public key must never verify
            {"iss": ISSUER, "aud": "asas", "iat": 1, "exp": 4102444800, "groups": ["ASAS-ADMINS"]},
            PUBLIC.public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode()[:64],
            algorithm="HS256",
        ),
        "not-a-jwt",
    ],
)
def test_oidc_rejects_bad_tokens(cfg: Config, token: str) -> None:
    with pytest.raises(AuthError):
        _oidc(cfg).authenticate(_bearer(token))


def test_oidc_refuses_weak_configuration_and_unentitled_users(cfg: Config) -> None:
    weak = _oidc_cfg(cfg).with_value(["HS256"], "security", "oidc", "algorithms")
    with pytest.raises(AuthError, match="asymmetric"):
        build_authenticator(weak, key_resolver=lambda _t: PUBLIC)
    with pytest.raises(EntitlementError):
        _oidc(cfg).authenticate(_bearer(_token(groups=["SOME-OTHER-GROUP"])))
    with pytest.raises(AuthError, match="Bearer"):
        _oidc(cfg).authenticate({"authorization": "Basic abc"})


def test_api_ignores_spoofed_headers_under_oidc(demo: DemoResult, cfg: Config) -> None:
    client = TestClient(create_app(demo.platform, authenticator=_oidc(cfg)))
    spoof = {"X-User": "approver.omar", "X-Roles": "admin"}
    assert client.get("/api/whoami", headers=spoof).status_code == 401
    ok = client.get("/api/whoami", headers={**spoof, **_bearer(_token())})
    assert ok.status_code == 200
    assert ok.json()["user"] == "jane.analyst" and ok.json()["roles"] == ["investigator"]
    # an analyst token cannot act as an approver whatever the headers say
    denied = client.post(
        "/api/policy/rollback",
        json={"bundle_id": "BUNDLE-0001", "reason": "x"},
        headers={**spoof, **_bearer(_token())},
    )
    assert denied.status_code == 403


# ---------------------------------------------------------------- trusted proxy


def test_proxy_identity_requires_the_shared_secret(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASAS_PROXY_SECRET", "s" * 40)
    proxy_cfg = cfg.with_value("proxy", "security", "mode")
    auth = TrustedProxyAuthenticator(proxy_cfg, Entitlements.from_config(proxy_cfg))
    forwarded = {"x-forwarded-user": "omar", "x-forwarded-groups": "ASAS-APPROVERS,ASAS-DESK-ALL"}
    with pytest.raises(AuthError):
        auth.authenticate(forwarded)  # reached the app directly, bypassing the proxy
    with pytest.raises(AuthError):
        auth.authenticate({**forwarded, "x-asas-proxy-secret": "wrong"})
    who = auth.authenticate({**forwarded, "x-asas-proxy-secret": "s" * 40})
    assert who.user_id == "omar" and who.has(Role.APPROVER) and who.sees_desk("FX")


# ---------------------------------------------------------------- readiness and HTTP


def test_production_refuses_insecure_configuration(demo: DemoResult, cfg: Config) -> None:
    prod = cfg.with_value("prod", "security", "environment")
    names = {c.name for c in checks(prod) if c.level == "FAIL"}
    assert {"auth", "audit_anchor"} <= names
    demo.platform.cfg = prod
    try:
        with pytest.raises(NotReady):
            create_app(demo.platform)
    finally:
        demo.platform.cfg = cfg


def test_production_starts_when_secure(
    demo: DemoResult, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASAS_AUDIT_SIGNING_KEY", "k")
    prod = (
        _oidc_cfg(cfg)
        .with_value("prod", "security", "environment")
        .with_value(str(tmp_path / "anchors"), "audit", "anchor_dir")
    )
    assert not [c for c in checks(prod) if c.level == "FAIL"]
    demo.platform.cfg = prod
    try:
        oidc = OidcAuthenticator(prod, Entitlements.from_config(prod), lambda _t: PUBLIC)
        client = TestClient(create_app(demo.platform, authenticator=oidc))
        response = client.get("/api/whoami", headers=_bearer(_token()))
        assert response.status_code == 200
        assert "max-age" in response.headers["strict-transport-security"]
    finally:
        demo.platform.cfg = cfg


def test_security_headers_request_id_body_limit_and_rate_limit(
    demo: DemoResult, cfg: Config
) -> None:
    demo.platform.cfg = cfg.with_value(3, "security", "rate_limit_per_minute").with_value(
        64, "security", "max_body_bytes"
    )
    try:
        client = TestClient(create_app(demo.platform))
        headers = {"X-User": "burst.user", "X-Roles": "viewer", "X-Request-ID": "req-123"}
        first = client.get("/api/whoami", headers=headers)
        assert first.headers["x-request-id"] == "req-123"
        assert "default-src 'self'" in first.headers["content-security-policy"]
        assert first.headers["x-frame-options"] == "DENY"
        assert first.headers["cache-control"] == "no-store"
        codes = [client.get("/api/whoami", headers=headers).status_code for _ in range(4)]
        assert codes[-1] == 429
        big = client.post(
            "/api/ops", headers={"X-User": "a", "X-Roles": "admin"}, content=b"x" * 500
        )
        assert big.status_code == 413
    finally:
        demo.platform.cfg = cfg


# ---------------------------------------------------------------- audit anchoring


def _rebuild_chain(db: Path, seq: int) -> None:
    """What an insider with database write access would do: drop the append-only triggers,
    rewrite one entry and recompute every later hash so the chain itself verifies again."""
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER audit_log_no_update")
    rows = conn.execute(
        "SELECT seq, at, actor, action, subject, detail FROM audit_log ORDER BY seq"
    ).fetchall()
    prev = "0" * 64
    for s, at, actor, action, subject, detail in rows:
        if s == seq:
            detail = canonical_json({"forged": True})
        body = canonical_json(
            {
                "at": at,
                "actor": actor,
                "action": action,
                "subject": subject,
                "detail": json.loads(detail),
                "prev": prev,
            }
        )
        digest = content_hash(body)
        conn.execute(
            "UPDATE audit_log SET detail=?, prev_hash=?, hash=? WHERE seq=?",
            (detail, prev, digest, s),
        )
        prev = digest
    conn.commit()
    conn.close()


def test_anchors_detect_a_consistently_rebuilt_history(demo: DemoResult, tmp_path: Path) -> None:
    db = tmp_path / "copy.db"
    with demo.platform.store.reader() as live, sqlite3.connect(db) as copy:
        live.raw.backup(copy)  # consistent online copy; the session store stays open
    private, _ = generate_ed25519_keypair()
    signer = Ed25519Signer(private)
    sink = FileAnchorSink(tmp_path / "anchors")
    store = Store(db)
    anchor = anchor_audit(store, signer, sink)
    assert anchor is not None
    assert verify_with_anchors(store, signer.public, sink.anchors()).ok
    store.close()

    _rebuild_chain(db, seq=3)
    tampered = Store(db)
    assert tampered.verify_audit_chain()[0]  # the chain alone is fooled
    report = verify_with_anchors(tampered, signer.public, sink.anchors())
    assert not report.ok and "history rewritten" in report.problems[0]
    other = Ed25519Signer(generate_ed25519_keypair()[0]).public
    assert "another key" in verify_with_anchors(tampered, other, sink.anchors()).problems[0]
    tampered.close()


def test_anchor_files_are_write_once(tmp_path: Path, db_path: Path) -> None:
    store = Store(db_path)
    store.audit("t", "A", "s", {})
    signer = HmacSigner(b"x" * 32)
    sink = FileAnchorSink(tmp_path / "a")
    first = anchor_audit(store, signer, sink)
    assert first is not None and anchor_audit(store, signer, sink) is not None  # idempotent
    path = next((tmp_path / "a").glob("anchor-*.json"))
    with pytest.raises(PermissionError):
        path.write_text("forged", encoding="utf-8")
    with pytest.raises(AnchorError):
        sink.write(first.model_copy(update={"hash": "f" * 64, "seq": first.seq}))
    with pytest.raises(AnchorError, match="32 bytes"):
        HmacSigner(b"short")
    store.close()


def test_pipeline_anchors_automatically_when_configured(
    demo: DemoResult, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = generate_ed25519_keypair()
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(private)
    monkeypatch.setenv("ASAS_AUDIT_SIGNING_KEY", str(key_file))
    demo.platform.cfg = cfg.with_value(str(tmp_path / "anchors"), "audit", "anchor_dir")
    try:
        demo.platform.run_pipeline(demo.dataset.end, SYSTEM)
        report = demo.platform.verify_audit()
        assert report.ok and report.anchors >= 1
    finally:
        demo.platform.cfg = cfg
