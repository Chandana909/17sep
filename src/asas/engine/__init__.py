"""Deterministic engine: linking, signals, rules, scoring, evidence, hypotheses, decisions.

Everything here is a pure function of typed inputs and versioned config. Agents call into
this layer through tools; they never compute facts themselves."""
