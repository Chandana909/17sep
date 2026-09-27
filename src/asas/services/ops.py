"""Operational controls: an audited safe-mode switch operations can flip without a deploy.

- `bulk_suspended`: no case may be proposed for bulk attestation (every case gets individual
  review with reason SAFE_MODE). Use it when data, a model or the policy is in doubt.
- `llm_suspended`: agents run on the deterministic playbook only (for example during a
  model incident, or while a new model version is evaluated).

Every change needs an admin, a reason, and lands in the hash-chained audit log. The state is
an append-only artifact history, so "who switched what, when and why" is always answerable.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from asas.core.errors import GovernanceError
from asas.core.security import Principal, Role, require_role
from asas.store.db import Store, ts_key, utcnow

KIND = "ops_state"
KEY = "current"


class OpsState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bulk_suspended: bool = False
    llm_suspended: bool = False
    reason: str = ""
    actor: str = "default"
    at: datetime | None = None

    def run_blocks(self) -> tuple[str, ...]:
        return (f"SAFE_MODE:{self.reason}",) if self.bulk_suspended else ()


class Ops:
    def __init__(self, store: Store) -> None:
        self._store = store

    def state(self) -> OpsState:
        found = self._store.get_model(KIND, KEY, OpsState)
        return found or OpsState()

    def history(self) -> list[OpsState]:
        return [
            OpsState.model_validate_json(payload)
            for _, _, payload in self._store.artifact_history(KIND, KEY)
        ]

    def set(
        self,
        principal: Principal,
        *,
        bulk_suspended: bool,
        llm_suspended: bool,
        reason: str,
    ) -> OpsState:
        require_role(principal, Role.ADMIN)
        if not reason.strip():
            raise GovernanceError("a safe-mode change needs a reason")
        now = utcnow()
        state = OpsState(
            bulk_suspended=bulk_suspended,
            llm_suspended=llm_suspended,
            reason=reason.strip()[:200],
            actor=principal.user_id,
            at=now,
        )
        self._store.put_artifact(KIND, KEY, ts_key(now), state)
        self._store.audit(
            principal.user_id,
            "OPS_STATE",
            KEY,
            {"bulk_suspended": bulk_suspended, "llm_suspended": llm_suspended, "reason": reason},
        )
        return state
