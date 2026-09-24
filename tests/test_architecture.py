"""Static architecture invariants: the deterministic boundary is enforced structurally."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "asas"
AGENT_PROGRAMS = [
    "agents/investigator.py",
    "agents/challenger.py",
    "agents/discovery.py",
    "agents/tools.py",
    "agents/memory.py",
    "agents/validation.py",
    "agents/prompts.py",
]
ENGINE = sorted(str(p.relative_to(SRC)).replace("\\", "/") for p in (SRC / "engine").glob("*.py"))
DECISION_POLICY = [
    "engine/decisions.py",
    "engine/hypotheses.py",
    "engine/scoring.py",
    "engine/linking.py",
    "engine/challenge.py",
    "engine/discovery.py",
]
PERSON_AND_WORKFLOW = {
    "TRADER_ID",
    "trader_id",
    "TRADER_REQUESTOR",
    "SUPERVISOR_GRP",
    "SUPERVISOR_GPN",
    "ALERT_GRP_ID",
    "RFI_FLAG",
    "SIGNOFF_COMMENTS",
    "persons",
    "workflow",
    "AlertAnnex",
    "TradePerson",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


@pytest.mark.parametrize("module", AGENT_PROGRAMS)
def test_agent_programs_have_no_io_or_write_capability(module: str) -> None:
    banned = (
        "os",
        "sqlite3",
        "socket",
        "subprocess",
        "urllib",
        "requests",
        "http",
        "shutil",
        "asas.store",
        "asas.evolution.governance",
        "asas.services",
    )
    imports = _imports(SRC / module)
    assert not [i for i in imports if i.split(".")[0] in banned or i.startswith(banned)], imports
    text = (SRC / module).read_text(encoding="utf-8")
    assert "open(" not in text and "eval(" not in text and "exec(" not in text


@pytest.mark.parametrize("module", ENGINE)
def test_engine_never_depends_on_agents_or_infrastructure(module: str) -> None:
    imports = _imports(SRC / module)
    assert not [
        i
        for i in imports
        if i.startswith(
            ("asas.agents", "asas.services", "asas.store", "asas.api", "sqlite3", "urllib")
        )
    ], imports


@pytest.mark.parametrize("module", DECISION_POLICY)
def test_decision_code_never_reads_person_or_workflow_fields(module: str) -> None:
    text = (SRC / module).read_text(encoding="utf-8")
    hits = [w for w in PERSON_AND_WORKFLOW if re.search(rf"\b{w}\b", text)]
    assert hits == []


@pytest.mark.parametrize(
    "module", ["engine/decisions.py", "engine/hypotheses.py", "engine/scoring.py"]
)
def test_policy_modules_take_thresholds_from_config(module: str) -> None:
    allowed = {0, 1, 2, 100, 3600}
    bad = []
    for node in ast.walk(ast.parse((SRC / module).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
            if isinstance(node.value, int | float) and node.value not in allowed:
                bad.append((node.lineno, node.value))
            if (
                isinstance(node.value, str)
                and re.fullmatch(r"-?\d+\.\d+", node.value)
                and node.value != "0.0001"
            ):
                bad.append((node.lineno, node.value))
    assert bad == []


def test_no_autonomous_business_actions_exist() -> None:
    pattern = re.compile(r"def\s+(sign_?off|send_?rfi|close_case|dispose|auto_?clear|suppress)\b")
    for path in SRC.rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


def test_frontend_escapes_untrusted_and_free_text_fields() -> None:
    js = (SRC / "api" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const esc = (v)" in js and "textContent = msg" in js
    for field in (
        "explanation_untrusted",
        "explanation",
        "rationale",
        "summary",
        "note",
        "detail",
        "facts",
        "description",
    ):
        for match in re.finditer(rf"\$\{{([^}}]*{field}[^}}]*)\}}", js):
            expr = match.group(1)
            assert "esc(" in expr, (field, expr)
