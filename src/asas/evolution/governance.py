"""Governance: the only path by which detection or bulk policy changes.

candidate -> replay -> counterexamples -> shadow -> human submit -> four-eyes approval ->
new immutable policy bundle -> activation. Every transition is an append-only event with the
hash of the artifact that justified it, plus an audit-chain entry. Agents hold no credential
for any of these methods: they require a human principal with the right role.
"""

from __future__ import annotations

import json

from asas.core.config import Config
from asas.core.errors import GovernanceError, PermissionDenied
from asas.core.ids import stable_id
from asas.core.security import Principal, Role, require_role
from asas.domain.models import (
    BulkPolicy,
    Candidate,
    CandidateEvent,
    CandidateKind,
    CandidateState,
    PolicyBundle,
    RuleSet,
)
from asas.store.db import Store, utcnow

ALLOWED: dict[CandidateState, set[CandidateState]] = {
    CandidateState.DRAFT: {CandidateState.REPLAYED, CandidateState.REJECTED},
    CandidateState.REPLAYED: {
        CandidateState.COUNTEREXAMPLES_PASSED,
        CandidateState.COUNTEREXAMPLES_FAILED,
        CandidateState.REJECTED,
    },
    CandidateState.COUNTEREXAMPLES_PASSED: {
        CandidateState.SHADOW_PASSED,
        CandidateState.SHADOW_FAILED,
        CandidateState.REJECTED,
    },
    CandidateState.COUNTEREXAMPLES_FAILED: {CandidateState.REJECTED},
    CandidateState.SHADOW_PASSED: {CandidateState.AWAITING_APPROVAL, CandidateState.REJECTED},
    CandidateState.SHADOW_FAILED: {CandidateState.REJECTED},
    CandidateState.AWAITING_APPROVAL: {CandidateState.RELEASED, CandidateState.REJECTED},
    CandidateState.RELEASED: set(),
    CandidateState.REJECTED: set(),
}


def _human(principal: Principal) -> None:
    is_service = Role.SERVICE in principal.roles and Role.ADMIN not in principal.roles
    if principal.user_id.startswith("agent:") or is_service:
        raise PermissionDenied("governance actions require a human principal")


class Governance:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg

    # ------------------------------------------------------------ bundles

    def seed(self, ruleset: RuleSet, bulk: BulkPolicy) -> PolicyBundle:
        existing = self.active_bundle_or_none()
        if existing is not None:
            return existing
        bundle = PolicyBundle(
            bundle_id="BUNDLE-0001",
            version=1,
            parent_id=None,
            ruleset=ruleset,
            bulk_policy=bulk,
            created_at=utcnow(),
            created_by="seed",
            source_candidate=None,
            notes="initial production policy",
        )
        self.store.put_artifact("policy_bundle", bundle.bundle_id, "1", bundle)
        self._activate(bundle.bundle_id, "seed")
        return bundle

    def bundle(self, bundle_id: str) -> PolicyBundle:
        found = self.store.get_model("policy_bundle", bundle_id, PolicyBundle)
        if found is None:
            raise GovernanceError(f"unknown bundle {bundle_id}")
        return found

    def active_bundle_or_none(self) -> PolicyBundle | None:
        payload = self.store.get_artifact("activation", "production")
        if payload is None:
            return None
        return self.bundle(json.loads(payload)["bundle_id"])

    def active_bundle(self) -> PolicyBundle:
        bundle = self.active_bundle_or_none()
        if bundle is None:
            raise GovernanceError("no active policy bundle; seed first")
        return bundle

    def bundles(self) -> list[PolicyBundle]:
        return [
            PolicyBundle.model_validate_json(p)
            for _, _, p in self.store.list_artifacts("policy_bundle")
        ]

    def activations(self) -> list[dict[str, str]]:
        return [
            json.loads(p) for _, _, p in self.store.artifact_history("activation", "production")
        ]

    def _activate(self, bundle_id: str, actor: str) -> None:
        seq = str(len(self.store.artifact_history("activation", "production")) + 1)
        self.store.put_artifact(
            "activation",
            "production",
            seq,
            {"bundle_id": bundle_id, "actor": actor, "at": utcnow().isoformat()},
        )
        self.store.audit(actor, "ACTIVATE_BUNDLE", bundle_id, {"sequence": seq})

    def rollback(self, bundle_id: str, principal: Principal, reason: str) -> PolicyBundle:
        require_role(principal, Role.APPROVER)
        _human(principal)
        bundle = self.bundle(bundle_id)
        self._activate(bundle_id, principal.user_id)
        self.store.audit(principal.user_id, "ROLLBACK", bundle_id, {"reason": reason})
        return bundle

    # ------------------------------------------------------------ candidates

    def create(self, candidate: Candidate) -> Candidate:
        existing = self.store.get_model("candidate", candidate.candidate_id, Candidate)
        if existing is not None:
            return existing
        self.store.put_artifact("candidate", candidate.candidate_id, "1", candidate)
        self._event(
            candidate.candidate_id,
            CandidateState.DRAFT,
            candidate.proposed_by,
            None,
            "proposed",
            check=False,
        )
        return candidate

    def candidate(self, candidate_id: str) -> Candidate:
        found = self.store.get_model("candidate", candidate_id, Candidate)
        if found is None:
            raise GovernanceError(f"unknown candidate {candidate_id}")
        return found

    def candidates(self) -> list[Candidate]:
        return [
            Candidate.model_validate_json(p) for _, _, p in self.store.list_artifacts("candidate")
        ]

    def events(self, candidate_id: str) -> list[CandidateEvent]:
        return [
            CandidateEvent.model_validate_json(p)
            for _, _, p in self.store.artifact_history("candidate_event", candidate_id)
        ]

    def state(self, candidate_id: str) -> CandidateState:
        events = self.events(candidate_id)
        if not events:
            raise GovernanceError(f"unknown candidate {candidate_id}")
        return events[-1].state

    def _event(
        self,
        candidate_id: str,
        state: CandidateState,
        actor: str,
        artifact_hash: str | None,
        note: str,
        check: bool = True,
    ) -> CandidateEvent:
        if check:
            current = self.state(candidate_id)
            if state not in ALLOWED[current]:
                raise GovernanceError(
                    f"{candidate_id}: {current.value} -> {state.value} not allowed"
                )
        seq = str(len(self.events(candidate_id)) + 1)
        event = CandidateEvent(
            candidate_id=candidate_id,
            state=state,
            actor=actor,
            at=utcnow(),
            artifact_hash=artifact_hash,
            note=note,
        )
        self.store.put_artifact("candidate_event", candidate_id, seq, event)
        self.store.audit(
            actor,
            f"CANDIDATE_{state.value}",
            candidate_id,
            {"artifact_hash": artifact_hash or "", "note": note},
        )
        return event

    def record_step(
        self,
        candidate_id: str,
        state: CandidateState,
        actor: str,
        artifact_kind: str,
        artifact_id: str,
        artifact: object,
        note: str,
    ) -> CandidateEvent:
        digest = self.store.put_artifact(artifact_kind, artifact_id, "1", artifact)
        return self._event(candidate_id, state, actor, digest, note)

    def submit(self, candidate_id: str, principal: Principal, note: str) -> CandidateEvent:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER)
        _human(principal)
        return self._event(
            candidate_id, CandidateState.AWAITING_APPROVAL, principal.user_id, None, note
        )

    def reject(self, candidate_id: str, principal: Principal, reason: str) -> CandidateEvent:
        require_role(principal, Role.APPROVER)
        _human(principal)
        return self._event(candidate_id, CandidateState.REJECTED, principal.user_id, None, reason)

    def approve(self, candidate_id: str, principal: Principal, note: str) -> PolicyBundle:
        require_role(principal, Role.APPROVER)
        _human(principal)
        if self.state(candidate_id) is not CandidateState.AWAITING_APPROVAL:
            raise GovernanceError("candidate is not awaiting approval")
        submitter = next(
            e.actor
            for e in reversed(self.events(candidate_id))
            if e.state is CandidateState.AWAITING_APPROVAL
        )
        if self.cfg.boolean("governance", "four_eyes") and submitter == principal.user_id:
            raise PermissionDenied("four-eyes: the submitter cannot approve their own submission")
        candidate = self.candidate(candidate_id)
        parent = self.active_bundle()
        if candidate.kind is CandidateKind.DETECTION_RULE and candidate.rule is not None:
            rules = tuple(r for r in parent.ruleset.rules if r.rule_id != candidate.rule.rule_id)
            ruleset = RuleSet(rules=(*rules, candidate.rule))
            bulk = parent.bulk_policy
        elif candidate.kind is CandidateKind.BULK_POLICY and candidate.bulk_scope is not None:
            ruleset = parent.ruleset
            scopes = tuple(s for s in parent.bulk_policy.allowed if s != candidate.bulk_scope)
            bulk = BulkPolicy(allowed=(*scopes, candidate.bulk_scope))
        else:
            raise GovernanceError("candidate has no payload")
        version = parent.version + 1
        bundle = PolicyBundle(
            bundle_id=f"BUNDLE-{version:04d}",
            version=version,
            parent_id=parent.bundle_id,
            ruleset=ruleset,
            bulk_policy=bulk,
            created_at=utcnow(),
            created_by=principal.user_id,
            source_candidate=candidate_id,
            notes=note,
        )
        self.store.put_artifact("policy_bundle", bundle.bundle_id, "1", bundle)
        self._event(
            candidate_id,
            CandidateState.RELEASED,
            principal.user_id,
            stable_id("BND", bundle.bundle_id),
            note,
        )
        self._activate(bundle.bundle_id, principal.user_id)
        return bundle
