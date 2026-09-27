# ADR 0014: Operational controls: safe mode, data gates, endpoint fallback, agent recovery

**Status:** accepted

**Decision.**
- **Safe mode** (`services/ops.py`): admin-only, needs a reason, audited. It suspends bulk proposals and/or the LLM without a deploy.
- **Data gates** (`engine/gates.py`): freshness and volume; a failed gate degrades the run and blocks bulk.
- **Model endpoints:** an ordered chain (`agents.fallbacks`), each with retries and a circuit breaker, then the playbook.
- **Agent recovery:**
  - after `agents.max_consecutive_rejections` stalled steps (rejected, failed or no-progress actions) the playbook finishes the run
  - a model may only abstain where the verifier would
  - the playbook completes a model's too-narrow hypothesis set
- **Pipeline:** a bounded worker pool with per-case failure isolation; failed runs are audited.

**Consequences.** Incidents degrade throughput and depth, never correctness or safety. These behaviours came from testing hijacked and looping models, including a real local Qwen, where a weak model otherwise burned its budget or steered to an abstention.
