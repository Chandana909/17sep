"""Infrastructure adapters: data sources (read-only) and model gateways.

Everything environment-specific lives here, behind the `ReadOnlySource` and `ModelGateway`
protocols, so the deterministic core never changes when data or models change."""
