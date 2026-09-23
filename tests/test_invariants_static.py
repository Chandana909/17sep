"""Static invariant checks over source code (rails 1, 8, 11, 12, 13)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from asas.fields import PERSON_FIELDS, WORKFLOW_FIELDS
from asas.sources import InMemorySource, ReadOnlySource

SRC = Path(__file__).resolve().parents[1] / "src" / "asas"
DECISION_MODULES = [
    "linking.py",
    "verification.py",
    "evidence.py",
    "treatment.py",
    "priority.py",
    "cases.py",
    "cohorts.py",
]
ALLOWED_INTS = {0, 1}
NUMERIC_STR = re.compile(r"^-?\d+(\.\d+)?$")


def _tree(name: str) -> ast.Module:
    return ast.parse((SRC / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", DECISION_MODULES)
def test_decision_modules_never_touch_workflow_or_person_fields(module: str) -> None:
    text = (SRC / module).read_text(encoding="utf-8")
    forbidden = WORKFLOW_FIELDS | PERSON_FIELDS | {"AlertAnnex", "annex", "workflow", "persons"}
    hits = [word for word in forbidden if re.search(rf"\b{word}\b", text, flags=re.IGNORECASE)]
    assert hits == []


@pytest.mark.parametrize("module", DECISION_MODULES)
def test_decision_modules_have_no_numeric_literals(module: str) -> None:
    bad = []
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
            if isinstance(node.value, int | float) and node.value not in ALLOWED_INTS:
                bad.append((node.lineno, node.value))
            if isinstance(node.value, str) and NUMERIC_STR.match(node.value):
                bad.append((node.lineno, node.value))
    assert bad == []


@pytest.mark.parametrize("module", DECISION_MODULES)
def test_decision_modules_are_pure(module: str) -> None:
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "print", "input", "eval", "exec"}
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not {"os", "sqlite3", "socket", "subprocess", "urllib", "requests"} & set(names)


def test_agent_runtime_has_no_io_or_write_capability() -> None:
    banned = {"os", "sqlite3", "socket", "subprocess", "urllib", "requests", "http", "shutil"}
    for path in (SRC / "agents").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import | ast.ImportFrom):
                roots = {(a.name or "").split(".")[0] for a in node.names}
                roots.add((getattr(node, "module", "") or "").split(".")[0])
                assert not banned & roots, path.name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "eval", "exec"}, path.name


@pytest.mark.parametrize("cls", [ReadOnlySource, InMemorySource])
def test_sources_expose_no_mutation(cls: type) -> None:
    verbs = ("insert", "update", "delete", "write", "execute", "commit", "save", "put", "set")
    public = [n for n in dir(cls) if not n.startswith("_")]
    assert not [n for n in public if n.startswith(verbs)]


def test_no_autonomous_business_actions_anywhere() -> None:
    pattern = re.compile(r"def\s+(sign_?off|send_?rfi|assign|reassign|dispose|close_case)\b")
    for path in SRC.rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name
