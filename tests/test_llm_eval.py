"""The real-model evaluation harness, exercised with scripted models."""

from __future__ import annotations

from pathlib import Path

from analysts import adaptive_analyst, garbage_model, injected_analyst
from asas.agents.gateway import ScriptedGateway
from asas.agents.runtime import fit_observation
from asas.core.config import Config
from asas.data.synthetic import SyntheticDataset
from asas.services.llm_eval import render_markdown, run_llm_eval


def test_a_capable_model_agrees_with_the_playbook(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    report = run_llm_eval(
        tmp_path,
        cfg,
        dataset,
        ScriptedGateway(adaptive_analyst, model_id="scripted-analyst"),
        scenarios=("FAT_FINGER", "OFF_MARKET", "CANCEL_REBOOK"),
    )
    s = report["summary"]
    assert s["cases"] >= 3 and s["agreement_with_playbook"].startswith(str(s["cases"]))
    assert s["valid_action_rate"] == "1.00" and s["fallback_steps"] == 0
    assert "| OFF_MARKET |" in render_markdown(report)


def test_a_broken_or_hijacked_model_changes_cost_not_outcomes(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    for script, name in ((garbage_model, "garbage"), (injected_analyst, "injected")):
        report = run_llm_eval(
            tmp_path / name,
            cfg,
            dataset,
            ScriptedGateway(script, model_id=name),
            scenarios=("FAT_FINGER", "OFF_MARKET"),
        )
        s = report["summary"]
        detail = [(c["scenario"], c["reference"], c["model"], c["fallbacks"]) for c in report["cases"]]
        assert s["agreement_with_playbook"].startswith(str(s["cases"])), (name, detail)
        assert s["fallback_steps"] > 0
        if name == "injected":
            assert s["rejected_conclusions"] > 0 and s["injection_resisted"] is not False


def test_long_observations_stay_valid_json() -> None:
    payload = {"episode_id": "E", "evidence": [{"id": i, "facts": "x" * 200} for i in range(50)]}
    text = fit_observation(payload, 2000)
    import json

    parsed = json.loads(text)
    assert len(text) <= 2000 and parsed["truncated"] is True
    assert parsed["evidence"][-1]["id"] == 49  # the most recent evidence is kept


def test_a_model_that_repeats_itself_hands_over_to_the_playbook(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    """A weak model looping on the same action (seen with small local models) stalls; after
    `agents.max_consecutive_rejections` no-progress steps the playbook finishes the run."""

    def parrot(request):  # type: ignore[no-untyped-def]
        import json

        payload = json.loads(request.user)
        return json.dumps(
            {
                "action": "call_tool",
                "tool": "get_episode",
                "args": {"episode_id": payload["episode_id"]},
                "purpose": "again",
            }
        )

    report = run_llm_eval(
        tmp_path, cfg, dataset, ScriptedGateway(parrot, model_id="parrot"), scenarios=("OFF_MARKET",)
    )
    case = report["cases"][0]
    assert case["agreed"] and case["model_calls"] <= cfg.integer("agents", "max_consecutive_rejections") + 1
