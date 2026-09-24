"""Typed error hierarchy. Every failure the platform can explain has a name."""

from __future__ import annotations


class AsasError(Exception):
    """Base class for platform errors."""


class ConfigMissing(AsasError, KeyError):
    """A decision-relevant config key is absent or malformed. Callers must fail safe."""


class DataContractError(AsasError, ValueError):
    """Input violates the field contract."""


class UnknownFieldError(DataContractError):
    def __init__(self, fields: list[str]) -> None:
        super().__init__(f"unknown fields not in contract: {sorted(fields)}")
        self.fields = sorted(fields)


class PointInTimeViolation(AsasError):
    """A read would observe data recorded after `as_of`."""


class PermissionDenied(AsasError):
    """The principal lacks the role or entitlement for the action."""


class GovernanceError(AsasError):
    """An evolution step was attempted out of order or without the required evidence."""


class ToolError(AsasError):
    """A tool call failed validation or execution."""


class ToolNotAllowed(ToolError):
    """The tool is not in the agent's granted capability set."""


class ModelError(AsasError):
    """The model gateway failed (timeout, transport, circuit open, bad payload)."""


class BudgetExceeded(AsasError):
    """An agent run exceeded one of its budgets."""


class ImmutableRecordError(AsasError):
    """An attempt was made to change an append-only record."""
