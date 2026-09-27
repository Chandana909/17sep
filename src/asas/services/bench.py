"""Performance benchmark: stage timings at growing volumes (`asas bench`).

Each scale multiplies every scenario rate of the synthetic generator (normal flow and every
planted scenario), then times the stages of a daily run on a fresh store:
- generate
- ingest
- snapshot (linking, signals, baselines, deviation profiles, scores, detections)
- deterministic assessment of every episode
- the pipeline over the review window (playbook investigations and decisions)
- the challenger

The numbers size a deployment and show where time goes. The LLM is off: model latency is
measured separately by `asas llm-eval`.
"""

from __future__ import annotations

import platform as host
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asas.core.config import Config
from asas.core.security import SYSTEM
from asas.data.synthetic import GeneratorSpec, generate
from asas.services.demo import ADMIN, ANALYST, ruleset_path
from asas.services.platform import Platform


def _scaled(spec: GeneratorSpec, scale: int) -> GeneratorSpec:
    return replace(
        spec,
        normal_per_day=spec.normal_per_day * scale,
        rates={k: v * scale for k, v in spec.rates.items()},
        novel_rates={k: v * scale for k, v in spec.novel_rates.items()},
        window_dressing_per_month_end=spec.window_dressing_per_month_end * scale,
    )


def run_bench(workdir: str | Path, cfg: Config, scales: list[int], days: int = 120) -> dict[str, Any]:
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    results = []
    for scale in scales:
        timings: dict[str, float] = {}

        def timed(name: str, fn: Any) -> Any:
            started = time.perf_counter()
            out = fn()
            timings[name] = round(time.perf_counter() - started, 2)
            return out

        dataset = timed("generate", lambda s=scale: generate(_scaled(GeneratorSpec(days=days), s)))
        platform = Platform(root / f"bench-{stamp}-x{scale}.db", cfg)
        platform.seed_policy(ruleset_path())
        timed("ingest", lambda: platform.ingest(dataset.bundle, ADMIN))
        snap = timed("snapshot", lambda: platform.snapshot(dataset.end))
        timed("assess_all", lambda: platform.assessments(dataset.end))
        report = timed("pipeline", lambda: platform.run_pipeline(dataset.end, SYSTEM))
        timed("challenge", lambda: platform.challenge(dataset.end, ANALYST))
        results.append(
            {
                "scale": scale,
                "trade_events": len(dataset.bundle.trade_events),
                "alerts": len(dataset.bundle.alerts),
                "episodes": len(snap.episodes),
                "cases": report.cases,
                "seconds": timings,
                "total_seconds": round(sum(timings.values()), 2),
                "ms_per_case_pipeline": round(1000 * timings["pipeline"] / max(report.cases, 1), 1),
            }
        )
        platform.store.close()
    return {
        "host": {
            "python": host.python_version(),
            "system": f"{host.system()} {host.release()}",
            "machine": host.machine(),
            "processor": host.processor(),
        },
        "days": days,
        "results": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    h = report["host"]
    stages = ("generate", "ingest", "snapshot", "assess_all", "pipeline", "challenge")
    lines = [
        f"Host: {h['system']} {h['machine']}, Python {h['python']}; {report['days']} days of data; "
        "SQLite store; LLM off (playbook).",
        "",
        "| scale | trade events | alerts | episodes | cases | "
        + " | ".join(f"{s} s" for s in stages)
        + " | total s | pipeline ms/case |",
        "|---|---|---|---|---|" + "---|" * len(stages) + "---|---|",
    ]
    for r in report["results"]:
        lines.append(
            f"| {r['scale']}x | {r['trade_events']} | {r['alerts']} | {r['episodes']} | {r['cases']} | "
            + " | ".join(str(r["seconds"][s]) for s in stages)
            + f" | {r['total_seconds']} | {r['ms_per_case_pipeline']} |"
        )
    return "\n".join(lines) + "\n"
