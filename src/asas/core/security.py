"""Principals, roles and entitlements. Enforced in services and tools, never in prompts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from asas.core.errors import PermissionDenied

ALL_DESKS = "*"


class Role(StrEnum):
    VIEWER = "viewer"
    INVESTIGATOR = "investigator"
    APPROVER = "approver"
    ADMIN = "admin"
    SERVICE = "service"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    roles: frozenset[Role]
    desks: frozenset[str] = frozenset({ALL_DESKS})
    person_data: bool = False

    def has(self, role: Role) -> bool:
        return role in self.roles or Role.ADMIN in self.roles

    def sees_desk(self, desk: str) -> bool:
        return ALL_DESKS in self.desks or desk in self.desks


def require_role(principal: Principal, *roles: Role) -> None:
    if not any(principal.has(r) for r in roles):
        wanted = ", ".join(r.value for r in roles)
        raise PermissionDenied(f"{principal.user_id} needs one of: {wanted}")


def require_desk(principal: Principal, desk: str) -> None:
    if not principal.sees_desk(desk):
        raise PermissionDenied(f"{principal.user_id} is not entitled to desk {desk}")


SYSTEM = Principal(user_id="system", roles=frozenset({Role.SERVICE}))


def agent_principal(agent: str, on_behalf_of: Principal) -> Principal:
    """Agents act with the requesting principal's data scope but never its approval rights."""
    return Principal(
        user_id=f"agent:{agent}",
        roles=frozenset({Role.SERVICE}),
        desks=on_behalf_of.desks,
        person_data=on_behalf_of.person_data,
    )
