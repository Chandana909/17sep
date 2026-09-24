"""HTTP API: integration of UI-facing endpoints with the platform, and role enforcement."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from asas.api.app import create_app
from asas.services.demo import DemoResult


def headers(user: str, role: str, desks: str = "*") -> dict[str, str]:
    return {"X-User": user, "X-Roles": role, "X-Desks": desks}


ANALYST = headers("analyst.jane", "investigator")
APPROVER = headers("approver.omar", "approver")
VIEWER = headers("viewer.ann", "viewer")


@pytest.fixture(scope="module")
def client(demo: DemoResult) -> TestClient:
    return TestClient(create_app(demo.platform))


def test_console_and_health(client: TestClient) -> None:
    assert "ASAS" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    health = client.get("/api/health").json()
    assert health["audit_chain_valid"] is True and health["audit_entries"] > 0


def test_overview_cases_and_case_detail(client: TestClient) -> None:
    overview = client.get("/api/overview", headers=ANALYST).json()
    assert overview["latest_run"]["cases"] > 0 and overview["active_bundle"].startswith("BUNDLE-")
    cases = client.get("/api/cases", headers=ANALYST).json()
    assert cases and {"PROPOSED_BULK", "ESCALATION_RECOMMENDED"} <= {
        c["recommendation"] for c in cases
    }
    bulk_only = client.get("/api/cases?recommendation=PROPOSED_BULK", headers=ANALYST).json()
    assert bulk_only and all(c["recommendation"] == "PROPOSED_BULK" for c in bulk_only)
    detail = client.get(f"/api/cases/{cases[0]['case_id']}", headers=ANALYST).json()
    assert detail["episode"]["episode_id"] == cases[0]["episode_id"]
    assert detail["investigation"]["hypotheses"] and detail["graph"]["nodes"]
    assert all(
        "<<<UNTRUSTED_TEXT>>>" in a["explanation_untrusted"]
        for a in detail["alerts"]
        if a["explanation_untrusted"]
    )
    trace = client.get(f"/api/runs/{detail['investigation']['run_id']}", headers=ANALYST).json()
    assert trace["manifest"]["tool_schema_version"] and trace["steps"]


def test_desk_entitlements_filter_cases(client: TestClient) -> None:
    fx = client.get("/api/cases", headers=headers("fx.user", "investigator", "FX")).json()
    assert fx and all(c["desk"] == "FX" for c in fx)
    eq_case = next(c for c in client.get("/api/cases", headers=ANALYST).json() if c["desk"] == "EQ")
    assert (
        client.get(
            f"/api/cases/{eq_case['case_id']}", headers=headers("fx.user", "investigator", "FX")
        ).status_code
        == 403
    )


def test_roles_are_enforced_server_side(client: TestClient) -> None:
    candidates = client.get("/api/candidates", headers=VIEWER).json()
    cid = candidates[0]["candidate"]["candidate_id"]
    assert (
        client.post(
            f"/api/candidates/{cid}/approve", headers=VIEWER, json={"note": "x"}
        ).status_code
        == 403
    )
    assert client.post("/api/pipeline/run", headers=VIEWER, json={}).status_code == 403
    released = client.post(
        f"/api/candidates/{cid}/approve", headers=APPROVER, json={"note": "again"}
    )
    assert released.status_code == 409  # already released: governance state machine holds


def test_policy_graph_patterns_audit(client: TestClient) -> None:
    policy = client.get("/api/policy", headers=ANALYST).json()
    assert len(policy["bundles"]) >= 3 and policy["activations"]
    assert client.get("/api/patterns", headers=ANALYST).json()
    cases = client.get("/api/cases", headers=ANALYST).json()
    graph = client.get(
        f"/api/graph/episode:{cases[0]['episode_id']}?depth=1", headers=ANALYST
    ).json()
    assert graph["nodes"]
    audit = client.get("/api/audit?limit=20", headers=ANALYST).json()
    assert audit["valid"] and len(audit["recent"]) == 20
    assert client.get("/api/evaluation", headers=ANALYST).json()["treatment"]


def test_human_decision_is_recorded_as_raw_outcome(client: TestClient, demo: DemoResult) -> None:
    case = client.get("/api/cases?recommendation=PROPOSED_BULK", headers=ANALYST).json()[0]
    outcomes = client.post(
        f"/api/cases/{case['case_id']}/decision", headers=ANALYST, json={"label": "CLEARED"}
    ).json()
    assert outcomes and all(o["quality"] == "RAW" for o in outcomes)
    own = client.post(
        f"/api/outcomes/{outcomes[0]['outcome_id']}/curate",
        headers=headers("analyst.jane", "approver"),
    )
    assert own.status_code == 403  # four-eyes on curation
    curated = client.post(
        f"/api/outcomes/{outcomes[0]['outcome_id']}/curate", headers=APPROVER
    ).json()
    assert curated["quality"] == "CURATED"
