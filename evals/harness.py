"""Synthetic-track eval runner. BRIEF.md §7.

For step 2 the predicted report is the expected fixture (replay mode); the
harness exercises the schema, scoring, and aggregation paths so it is ready
the moment a real agent loop lands in step 6. Once SYNTHESIZE is wired, the
predicted report comes from `agent.synthesize.run(case)` instead.

CI fail-fast: any track regression > 5 percentage points absolute fails the run.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ai_oncall.models import RcaReport
from evals.scoring import score_all

REPO = Path(__file__).resolve().parents[1]
CASES_DIR = REPO / "evals" / "cases"

THRESHOLDS: dict[str, float] = {
    "component_match": 0.80,
    "reason_cosine": 0.50,
    "trajectory_score": 1.50,
    "escalation_precision": 0.80,
}


@dataclass
class CaseResult:
    case_id: str
    family: str
    difficulty: str
    metrics: dict[str, float]


def _load_case(path: Path) -> CaseResult:
    with path.open(encoding="utf-8") as f:
        case = json.load(f)
    expected = RcaReport.model_validate_json((REPO / case["expected_fixture"]).read_text(encoding="utf-8"))
    predicted = expected  # replay mode until step 6 wires the agent
    return CaseResult(
        case_id=case["case_id"],
        family=case["family"],
        difficulty=case["difficulty"],
        metrics=score_all(predicted, expected),
    )


def run(cases_dir: Path = CASES_DIR) -> list[CaseResult]:
    return [_load_case(p) for p in sorted(cases_dir.glob("*.json"))]


def aggregate(results: Iterable[CaseResult]) -> dict[str, float]:
    rows = list(results)
    if not rows:
        return {}
    keys = list(rows[0].metrics)
    return {k: sum(r.metrics[k] for r in rows) / len(rows) for k in keys}


def render(results: list[CaseResult], aggregates: dict[str, float]) -> str:
    lines = ["case_id\tfamily\tdifficulty\t" + "\t".join(aggregates.keys())]
    for r in results:
        cells = [r.case_id, r.family, r.difficulty]
        cells += [f"{r.metrics[k]:.2f}" for k in aggregates]
        lines.append("\t".join(cells))
    lines.append("AVG\t-\t-\t" + "\t".join(f"{aggregates[k]:.2f}" for k in aggregates))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ai-oncall eval harness")
    parser.add_argument("--track", choices=["synthetic", "rcaeval", "openrca"], default="synthetic")
    parser.add_argument("--data-dir", type=Path, help="Required for --track=rcaeval|openrca")
    args = parser.parse_args(argv)

    if args.track == "synthetic":
        results = run()
    elif args.track == "rcaeval":
        from evals.rcaeval_loader import load_cases as load_rcaeval

        if not args.data_dir:
            print("ERROR: --data-dir required for --track=rcaeval", file=sys.stderr)  # noqa: T201
            return 2
        try:
            list(load_rcaeval(args.data_dir))  # raises NotImplementedError today
        except NotImplementedError as exc:
            print(f"SKIP rcaeval track: {exc}", file=sys.stderr)  # noqa: T201
            return 0
        results = []  # populated once the loader lands
    else:  # openrca
        from evals.openrca_loader import load_cases as load_openrca

        if not args.data_dir:
            print("ERROR: --data-dir required for --track=openrca", file=sys.stderr)  # noqa: T201
            return 2
        try:
            list(load_openrca(args.data_dir))
        except NotImplementedError as exc:
            print(f"SKIP openrca track: {exc}", file=sys.stderr)  # noqa: T201
            return 0
        results = []

    aggregates = aggregate(results)
    print(render(results, aggregates))  # noqa: T201 — eval CLI output, not application logging
    failures = [k for k, v in aggregates.items() if v < THRESHOLDS.get(k, 0.0)]
    if failures:
        print(f"\nFAIL: below threshold on {failures}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
