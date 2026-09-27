"""Authentication: who is calling, established from a verified source, never from the body.

Three modes (`security.mode`):

- **dev:** identity from `X-User` / `X-Roles` / `X-Desks` headers. Convenient locally, trivially
  spoofable, therefore refused when `security.environment = "prod"` (see core/readiness.py).
- **proxy:** an authenticating reverse proxy (oauth2-proxy, an API gateway, Apache mod_auth)
  forwards the user and groups. The headers are trusted **only** when the request also
  carries the proxy's shared secret (constant-time comparison), so a caller who reaches the
  app directly cannot forge an identity.
- **oidc:** a signed JWT access or ID token from the bank's identity provider, verified
  against its JWKS with PyJWT:
  - asymmetric algorithms only (no `none`, no HMAC)
  - issuer, audience, expiry and not-before checked with a small leeway

Groups map to roles (`security.role_map`) and desks (`security.desk_map`); a principal with
no mapped role has no access (least privilege). Agents never authenticate: they run under
`agent_principal`, which has no approval rights.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from asas.core.config import Config
from asas.core.errors import AsasError
from asas.core.security import ALL_DESKS, Principal, Role

ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


class AuthError(AsasError):
    """The request could not be authenticated (HTTP 401)."""


class EntitlementError(AsasError):
    """Authenticated, but not entitled to use ASAS at all (HTTP 403)."""


class Authenticator(Protocol):
    mode: str

    def authenticate(self, headers: Mapping[str, str]) -> Principal: ...


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Entitlements:
    """Directory groups -> ASAS roles, desks and person-data entitlement."""

    role_map: Mapping[str, str] = field(default_factory=dict)
    desk_map: Mapping[str, str] = field(default_factory=dict)
    person_data_groups: frozenset[str] = frozenset()

    @staticmethod
    def from_config(cfg: Config) -> Entitlements:
        return Entitlements(
            role_map=dict(cfg.section("security", "role_map")),
            desk_map=dict(cfg.section("security", "desk_map")),
            person_data_groups=frozenset(cfg.strings("security", "person_data_groups")),
        )

    def principal(self, user: str, groups: Iterable[str]) -> Principal:
        if not user.strip():
            raise AuthError("no user identity")
        members = set(groups)
        roles = frozenset(
            Role(self.role_map[g])
            for g in members
            if g in self.role_map and self.role_map[g] in Role._value2member_map_
        )
        if not roles:
            raise EntitlementError(f"{user} has no ASAS role (check security.role_map)")
        desks = frozenset(self.desk_map[g] for g in members if g in self.desk_map)
        return Principal(
            user_id=user.strip(),
            roles=roles,
            desks=desks,
            person_data=bool(members & self.person_data_groups),
        )


class DevHeaderAuthenticator:
    mode = "dev"

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        roles = frozenset(
            Role(r)
            for r in _split(headers.get("x-roles", "viewer"))
            if r in Role._value2member_map_
        )
        desks = frozenset(_split(headers.get("x-desks", ALL_DESKS)))
        return Principal(
            user_id=headers.get("x-user", "anonymous"),
            roles=roles or frozenset({Role.VIEWER}),
            desks=desks or frozenset({ALL_DESKS}),
            person_data=headers.get("x-person-data", "false") == "true",
        )


class TrustedProxyAuthenticator:
    mode = "proxy"

    def __init__(self, cfg: Config, entitlements: Entitlements) -> None:
        section = cfg.section("security", "proxy")
        secret = os.environ.get(str(section["secret_env"]), "")
        if not secret:
            raise AuthError(f"proxy mode needs the shared secret in ${section['secret_env']}")
        self._secret = secret.encode("utf-8")
        self._secret_header = str(section["secret_header"]).lower()
        self._user_header = str(section["user_header"]).lower()
        self._groups_header = str(section["groups_header"]).lower()
        self._entitlements = entitlements

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        presented = headers.get(self._secret_header, "").encode("utf-8")
        if not presented or not hmac.compare_digest(presented, self._secret):
            raise AuthError("request did not come through the authenticating proxy")
        return self._entitlements.principal(
            headers.get(self._user_header, ""), _split(headers.get(self._groups_header))
        )


class OidcAuthenticator:
    mode = "oidc"

    def __init__(
        self, cfg: Config, entitlements: Entitlements, key_resolver: Any | None = None
    ) -> None:
        import jwt

        section = cfg.section("security", "oidc")
        algorithms = [str(a) for a in section["algorithms"]]
        weak = sorted(set(algorithms) - ASYMMETRIC_ALGORITHMS)
        if weak or not algorithms:
            raise AuthError(f"oidc algorithms must be asymmetric; refused {weak or 'none'}")
        self._jwt = jwt
        self._algorithms = algorithms
        self._issuer = str(section["issuer"])
        self._audience = str(section["audience"])
        self._leeway = int(section["leeway_seconds"])
        self._user_claim = str(section["user_claim"])
        self._groups_claim = str(section["groups_claim"])
        self._token_header = str(section["token_header"]).lower()
        # key_resolver(token) -> key; default fetches and caches the IdP's JWKS
        self._resolve = (
            key_resolver
            or jwt.PyJWKClient(str(section["jwks_url"]), cache_keys=True).get_signing_key_from_jwt
        )
        self._entitlements = entitlements

    def _token(self, headers: Mapping[str, str]) -> str:
        raw = headers.get(self._token_header, "").strip()
        if self._token_header == "authorization":
            scheme, _, value = raw.partition(" ")
            if scheme.lower() != "bearer" or not value:
                raise AuthError("expected 'Authorization: Bearer <token>'")
            return value.strip()
        if not raw:
            raise AuthError(f"missing token header {self._token_header}")
        return raw

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        token = self._token(headers)
        jwt = self._jwt
        try:
            key = self._resolve(token)
            claims = jwt.decode(
                token,
                key=getattr(key, "key", key),
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {type(exc).__name__}") from exc
        groups = claims.get(self._groups_claim, [])
        if isinstance(groups, str):
            groups = _split(groups)
        user = claims.get(self._user_claim) or claims.get("sub") or ""
        return self._entitlements.principal(str(user), [str(g) for g in groups])


def build_authenticator(cfg: Config, key_resolver: Any | None = None) -> Authenticator:
    mode = cfg.string("security", "mode")
    if mode == "dev":
        return DevHeaderAuthenticator()
    entitlements = Entitlements.from_config(cfg)
    if mode == "proxy":
        return TrustedProxyAuthenticator(cfg, entitlements)
    if mode == "oidc":
        return OidcAuthenticator(cfg, entitlements, key_resolver)
    raise AuthError(f"unknown security.mode {mode!r} (dev | proxy | oidc)")
