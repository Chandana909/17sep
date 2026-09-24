"""Agent runtime and agent programs (Investigator, Challenger, Discovery).

Agents reason and choose what to investigate. They hold no write credentials, can only call
the closed registry of read-only tools, and every claim they make is checked by
deterministic code before it counts."""
