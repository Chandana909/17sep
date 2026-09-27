"""Deployment readiness: settings that are fine on a laptop and unacceptable in production.

`checks()` is the single list behind `asas doctor` and the API start-up guard. With
`security.environment = "prod"` any FAIL refuses to start the service (fail closed), so an
insecure configuration cannot be deployed by accident. WARN items are allowed but reported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from asas.core.config import Config
from asas.core.errors import AsasError

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class NotReady(AsasError):
    """Production start-up refused: at least one readiness check failed."""


@dataclass(frozen=True)
class Check:
    name: str
    level: str  # OK | WARN | FAIL
    detail: str


def _prod(cfg: Config) -> bool:
    return cfg.string("security", "environment") == "prod"


def checks(cfg: Config, database_url: str = "") -> list[Check]:
    prod = _prod(cfg)
    bad = "FAIL" if prod else "WARN"
    out: list[Check] = []

    mode = cfg.string("security", "mode")
    if mode == "dev":
        out.append(Check("auth", bad, "dev header identities are spoofable; use proxy or oidc"))
    else:
        out.append(Check("auth", "OK", f"mode {mode}"))
    if mode == "proxy":
        env = cfg.string("security", "proxy", "secret_env")
        present = bool(os.environ.get(env))
        out.append(
            Check(
                "proxy_secret",
                "OK" if present else "FAIL",
                f"${env} {'set' if present else 'missing'}",
            )
        )
    if mode == "oidc":
        jwks = urlparse(cfg.string("security", "oidc", "jwks_url"))
        ok = jwks.scheme == "https"
        out.append(Check("oidc_jwks", "OK" if ok else bad, f"JWKS over {jwks.scheme or 'nothing'}"))

    if cfg.boolean("agents", "enabled"):
        endpoints = [cfg.string("agents", "base_url")] + [
            str(f["base_url"]) for f in cfg.require("agents", "fallbacks")
        ]
        for url in endpoints:
            parsed = urlparse(url)
            local = parsed.hostname in LOCAL_HOSTS
            secure = parsed.scheme == "https" or local
            out.append(
                Check(
                    "model_endpoint",
                    "OK" if secure else bad,
                    f"{url} ({'local' if local else parsed.scheme})",
                )
            )
    else:
        out.append(Check("model_endpoint", "OK", "LLM disabled: deterministic playbook"))

    anchor_dir = cfg.string("audit", "anchor_dir")
    key_env = cfg.string("audit", "key_env")
    if not anchor_dir:
        out.append(
            Check("audit_anchor", bad, "audit.anchor_dir not set: chain is not externally anchored")
        )
    elif not os.environ.get(key_env):
        out.append(Check("audit_anchor", bad, f"${key_env} missing: cannot sign anchors"))
    else:
        out.append(Check("audit_anchor", "OK", f"anchoring to {anchor_dir}"))

    scheme = urlparse(database_url).scheme if "://" in database_url else "sqlite"
    if scheme.startswith("postgres"):
        out.append(Check("storage", "OK", "postgres"))
    else:
        out.append(
            Check(
                "storage", "WARN" if prod else "OK", "sqlite: single writer; use postgres for prod"
            )
        )

    rate = cfg.integer("security", "rate_limit_per_minute")
    out.append(Check("rate_limit", "OK" if rate > 0 else bad, f"{rate} requests/min/principal"))
    return out


def enforce(cfg: Config, database_url: str = "") -> list[Check]:
    results = checks(cfg, database_url)
    failed = [c for c in results if c.level == "FAIL"]
    if _prod(cfg) and failed:
        raise NotReady("; ".join(f"{c.name}: {c.detail}" for c in failed))
    return results
