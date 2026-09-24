# ADR 0008: Console served by the API, no separate build

**Status:** accepted

**Decision.** A dependency-free ES-module console is served by FastAPI and calls only `/api`. Every dynamic value is HTML-escaped (a static test checks that untrusted fields pass through `esc()`). Identity is sent as headers under `security.dev_auth`; production puts an OIDC proxy in front.

**Consequences.** One deployable unit; the UI is integrated with the real backend rather than a mock.
