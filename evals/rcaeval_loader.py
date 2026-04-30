"""RCAEval RE3-OB loader.

Purpose: convert RCAEval's RE3-OB benchmark into our `(Alert, RcaReport)` pair
format so the eval harness can score the agent against real telemetry traces
on the same 4 metrics (component_match, reason_cosine, trajectory_score,
escalation_precision).

Status: documented stub. Implementation lands when (a) the upstream RCAEval
data layout has been read directly from their public docs, and (b) we have
written our own loader (BRIEF.md §12 forbids copying upstream code).

Expected data layout (read from upstream docs, summarized for orientation):
- One sub-directory per scenario, named by injected fault.
- Each scenario contains observability dumps (metrics.csv, logs.csv, traces.csv)
  and a `gt.json` with the labelled root-cause service.
- Time windows are typically 30 min around the injected fault.

Mapping to our schemas:
- `gt.json` -> RcaReport.hypotheses[0].root_cause_service (component_match)
- `gt.json.reason` (free text) -> RcaReport.hypotheses[0].reasoning (reason_cosine)
- The injected-fault label is the trajectory ground truth — but RCAEval does
  NOT publish a reference tool-call sequence, so trajectory_score from RCAEval
  is graded by the LLM-as-judge rubric, not exact-match.

Documented gaps:
1. RCAEval scenarios assume a static topology. Our agent's live topology
   builder is not exercised here — pin to topology.yaml when running RCAEval.
2. Some RCAEval scenarios are very short (<5 minutes); our 30-min freshness
   budget (BRIEF.md §3) over-fetches in that case. Loader should clamp.
3. The original benchmark uses microservice names that don't match ours;
   the loader must namespace under `tenant_id="rcaeval"` to avoid colliding
   with synthetic-track services (`payment`, `checkout`, etc.).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ai_oncall.models import Alert, RcaReport


def load_cases(_data_dir: Path) -> Iterator[tuple[Alert, RcaReport]]:
    raise NotImplementedError(
        "RCAEval loader: implementation deferred until a customer needs the "
        "real-data eval (BRIEF.md §11 step 9). Reimplement the data-layout "
        "reader from upstream docs — do NOT vendor upstream code (§12)."
    )
