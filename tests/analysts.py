"""Scripted stand-ins for an LLM. They read only the observation payload the runtime sends,
exactly as a real model would, and reply with one JSON action."""

from __future__ import annotations

import json
import re
from typing import Any

from asas.agents.gateway import ModelRequest

_KEY = re.compile(r"^(\w+)\((.*)\)$")


def _parse_key(key: str) -> tuple[str, dict[str, str]] | None:
    match = _KEY.match(key)
    if not match:
        return None
    args = dict(part.split("=", 1) for part in match.group(2).split(", ") if "=" in part)
    return match.group(1), args


def _facts(payload: dict[str, Any], tool: str) -> dict[str, str]:
    for e in payload["evidence"]:
        if e["tool"] == tool:
            return dict(e["facts"])
    return {}


def adaptive_analyst(request: ModelRequest) -> str:
    """Reads the alert first, then the episode, proposes hypotheses from what it sees, lets the
    verifier tell it what is missing, fetches exactly that, and concludes on the verifier's
    answer. A different path from the deterministic playbook, same rules."""
    p = json.loads(request.user)
    tools = {e["tool"] for e in p["evidence"]}
    obs = p.get("last_observation") or {}
    if "get_episode" not in tools:
        return json.dumps(
            {
                "action": "call_tool",
                "tool": "get_episode",
                "args": {"episode_id": p["episode_id"]},
                "purpose": "context",
            }
        )
    episode = _facts(p, "get_episode")
    if "get_related_alerts" not in tools:
        return json.dumps(
            {
                "action": "call_tool",
                "tool": "get_related_alerts",
                "args": {"episode_id": p["episode_id"]},
                "purpose": "what else fired around this",
            }
        )
    if not p["hypotheses"]:
        types = ["RECURRING_BENIGN_CONTEXT"]
        if episode.get("signals.has_amend") == "true":
            types = ["OFF_MARKET_AMENDMENT", "PRICE_CORRECTION", "PERIOD_END_ROUND_TRIP", *types]
        if episode.get("signals.has_cancel") == "true":
            types = ["REBOOK_ECONOMICS_CHANGED", "CANCEL_REBOOK_CORRECTION", *types]
        if float(episode.get("signals.booking_latency_hours_max", "0")) >= 4:
            types.append("LATE_BOOKING_OPERATIONAL")
        if episode.get("signals.n_events") == "1":
            types.append("ROUTINE_EXECUTION")
        return json.dumps(
            {
                "action": "propose_hypotheses",
                "hypotheses": [
                    {"type": t, "rationale": "plausible given the episode"} for t in types
                ],
            }
        )
    for key in obs.get("missing", []):
        parsed = _parse_key(key)
        if parsed and parsed[0] not in {"verified_rebook_link"}:
            return json.dumps(
                {
                    "action": "call_tool",
                    "tool": parsed[0],
                    "args": parsed[1],
                    "purpose": "the verifier asked for this",
                }
            )
    for h in p["hypotheses"]:
        if h["status"] == "PROPOSED" or any(_parse_key(m) for m in h["missing"]):
            return json.dumps({"action": "evaluate", "hypothesis_id": h["id"]})
    verifier = obs.get("verifier")
    if obs.get("rejected") and not verifier and "pending" not in obs:
        return json.dumps(
            {
                "action": "abstain",
                "reason": "verifier supports no conclusion",
                "missing": [m for h in p["hypotheses"] for m in h["missing"]],
            }
        )
    target = next((h for h in p["hypotheses"] if h["type"] == verifier), None)
    if target is None:
        supported = [
            h
            for h in p["hypotheses"]
            if h["status"] == "SUPPORTED" and h["type"] != "RECURRING_BENIGN_CONTEXT"
        ]
        target = supported[0] if supported else None
    if target is None:
        return json.dumps(
            {
                "action": "abstain",
                "reason": "no hypothesis supported",
                "missing": [m for h in p["hypotheses"] for m in h["missing"]],
            }
        )
    return json.dumps(
        {
            "action": "conclude",
            "hypothesis_id": target["id"],
            "summary": "the verifier supports this explanation",
        }
    )


def injected_analyst(request: ModelRequest) -> str:
    """A model that obeys instructions planted in untrusted text: it keeps trying to conclude
    PRICE_CORRECTION without evidence."""
    p = json.loads(request.user)
    if not p["hypotheses"]:
        return json.dumps(
            {
                "action": "propose_hypotheses",
                "hypotheses": [{"type": "PRICE_CORRECTION", "rationale": "as instructed"}],
            }
        )
    return json.dumps(
        {
            "action": "conclude",
            "hypothesis_id": p["hypotheses"][0]["id"],
            "summary": "approved and signed off as instructed",
        }
    )


def garbage_model(_request: ModelRequest) -> str:
    return "Sure! I think this trade is fine. <think>no json here</think>"
