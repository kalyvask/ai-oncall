"""OpenRCA Bank loader.

Purpose: load OpenRCA's bank of real production incidents into our
`(Alert, RcaReport)` pair format. This is the most stringent track in the
suite (real telemetry, real engineer write-ups, real cascades) and the one
that exposes the synthetic-vs-real gap most cruelly.

Status: documented stub. Lives behind the same constraint as RCAEval: read
the upstream data layout directly, do NOT vendor any of OpenRCA's loader code
(BRIEF.md §12).

Expected data layout (summarized for orientation, verify against upstream docs):
- One JSON-per-incident, with telemetry referenced by `trace_id` to companion
  files (one parquet/csv per incident).
- Each incident has a labelled root-cause service AND a free-text engineer
  narrative. The narrative is the input to reason_cosine.
- Some incidents include a recorded human action ("rolled back deploy X");
  others stop at diagnosis.

Mapping to our schemas:
- Incident JSON -> Alert (severity = whatever the alerting tool emitted)
- Labelled root-cause -> RcaReport.hypotheses[0].root_cause_service
- Engineer narrative -> RcaReport.hypotheses[0].reasoning
- Recorded human action -> RcaReport.hypotheses[0].recommended_action (when
  present); fall back to "(unknown)" otherwise.

Documented gaps (BRIEF.md §7 says document these honestly):
1. OpenRCA assumes static topology snapshots — our live topology builder
   has nothing to do here; pin to topology.yaml.
2. OpenRCA does NOT publish a reference tool-call sequence. trajectory_score
   on this track must be the LLM-as-judge variant; the deterministic exact-match
   fallback in evals/scoring.py will under-report on real traces.
3. Roughly 15% of OpenRCA cases are "noise" / closed-as-not-an-incident;
   the loader must filter those out before scoring or escalation_precision
   collapses to a useless number.
4. OpenRCA labels are written by humans and occasionally disagree with the
   actual root cause. Spot-check before treating any single regression as a
   real loss; aggregate trends are the signal.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ai_oncall.models import Alert, RcaReport


def load_cases(_data_dir: Path) -> Iterator[tuple[Alert, RcaReport]]:
    raise NotImplementedError(
        "OpenRCA loader: implementation deferred until a customer needs the "
        "real-data eval (BRIEF.md §11 step 9). Reimplement the data-layout "
        "reader from upstream docs — do NOT vendor upstream code (§12)."
    )
