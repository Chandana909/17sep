# ADR 0011: Verified identity: OIDC or an authenticating proxy; dev headers only locally

**Status:** accepted (supersedes the identity part of ADR 0008)

**Context.** Four-eyes approval is only as strong as identity. Header-supplied identities are trivially spoofable.

**Decision.** `core/auth.py` offers three modes:
- `dev`: headers, refused in prod
- `proxy`: forwarded identity trusted only with the proxy's shared secret (constant-time compare)
- `oidc`: PyJWT against the IdP's JWKS; asymmetric algorithms only; iss, aud, exp and iat required

Directory groups map to roles and desks; no mapped role means no access. `core/readiness.py` refuses a prod start with dev auth or other insecure settings.

**Consequences.** It plugs into the bank's IdP without code changes. Local development stays frictionless. The rate limiter is per process, so replicas rely on the gateway for global limits.
