# ADR 0016: Business context catalogue; a restrained, CSP-strict console

**Status:** accepted (updates ADR 0008)

**Decision.**
- **Catalogue:** `config/business_context.toml` holds the plain-language meaning, risk theme and reviewer checks for every hypothesis, signal, rule, reason, category, corroboration line, recommendation and finding. Compliance owns the wording and a version number, and tests fail if any decision element lacks it. It never changes decisions; agents receive the risk themes as prompt context.
- **Console palette:** red for risk and attention, black for actions and verified states, grey for structure and benign or pending states, and white. States are always written out, never colour-only.
- **No inline resources:** no inline scripts or styles and nothing remote, so the CSP is `script-src 'self'; style-src 'self'`.
- **Changes:** business rule changes enter as human proposals (`asas propose`, `POST /api/candidates`) into the same governed chain as discovered candidates.

**Consequences.** Business changes to wording are a TOML edit, and rule changes are a governed proposal. The palette and CSP rules are enforced by architecture tests.
