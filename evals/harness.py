"""Synthetic-track eval runner. BRIEF.md §7.

For step 2 the predicted report is the expected fixture (replay mode); the
harness exercises the schema, scoring, and aggregation paths so it is ready
the moment a real agent loop lands in step 6. Once SYNTHESIZE is wired, the
predicted report comes from `agent.synthesize.run(case)` instead.

CI fail-fast: any track regression > 5 percentage points absolute fails the
run. The same threshold is used by `--baseline` to compare a current run
against a previous JSON snapshot and surface per-metric per-family drops.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
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

REGRESSION_THRESHOLD = 0.05  # 5 percentage points absolute


@dataclass
class CaseResult:
    case_id: str
    family: str
    difficulty: str
    metrics: dict[str, float]


@dataclass(frozen=True)
class Regression:
    scope: str  # "global" or a family name
    metric: str
    baseline: float
    current: float
    delta: float


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


def aggregate_by_family(results: Iterable[CaseResult]) -> dict[str, dict[str, float]]:
    by_family: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_family[r.family].append(r)
    return {family: aggregate(rows) for family, rows in by_family.items()}


def to_report(track: str, results: list[CaseResult]) -> dict:
    return {
        "track": track,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "results": [
            {
                "case_id": r.case_id,
                "family": r.family,
                "difficulty": r.difficulty,
                "metrics": r.metrics,
            }
            for r in results
        ],
        "aggregates_global": aggregate(results),
        "aggregates_by_family": aggregate_by_family(results),
    }


def diff_against_baseline(
    current: dict, baseline: dict, threshold: float = REGRESSION_THRESHOLD
) -> list[Regression]:
    """Return regressions where any metric drops by more than `threshold`."""
    out: list[Regression] = []
    cur_global = current.get("aggregates_global", {})
    base_global = baseline.get("aggregates_global", {})
    for metric, cur_v in cur_global.items():
        base_v = base_global.get(metric)
        if base_v is None:
            continue
        delta = cur_v - base_v
        if delta < -threshold:
            out.append(Regression("global", metric, base_v, cur_v, delta))
    cur_by_family = current.get("aggregates_by_family", {})
    base_by_family = baseline.get("aggregates_by_family", {})
    for family, cur_metrics in cur_by_family.items():
        base_family = base_by_family.get(family, {})
        for metric, cur_v in cur_metrics.items():
            base_v = base_family.get(metric)
            if base_v is None:
                continue
            delta = cur_v - base_v
            if delta < -threshold:
                out.append(Regression(family, metric, base_v, cur_v, delta))
    return out


def render(results: list[CaseResult], aggregates: dict[str, float]) -> str:
    lines = ["case_id\tfamily\tdifficulty\t" + "\t".join(aggregates.keys())]
    for r in results:
        cells = [r.case_id, r.family, r.difficulty]
        cells += [f"{r.metrics[k]:.2f}" for k in aggregates]
        lines.append("\t".join(cells))
    lines.append("AVG\t-\t-\t" + "\t".join(f"{aggregates[k]:.2f}" for k in aggregates))
    return "\n".join(lines)


def render_regressions(regressions: list[Regression]) -> str:
    lines = ["scope\tmetric\tbaseline\tcurrent\tdelta"]
    for r in sorted(regressions, key=lambda x: (x.scope, x.metric)):
        lines.append(
            f"{r.scope}\t{r.metric}\t{r.baseline:.3f}\t{r.current:.3f}\t{r.delta:+.3f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ai-oncall eval harness")
    parser.add_argument("--track", choices=["synthetic", "rcaeval", "openrca"], default="synthetic")
    parser.add_argument("--data-dir", type=Path, help="Required for --track=rcaeval|openrca")
    parser.add_argument(
        "--emit-json",
        type=Path,
        help="Write the full structured result (results + global + per-family aggregates) to this path.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Compare current run against this previous JSON snapshot. "
             "Exits non-zero on any per-metric per-family drop > --regression-threshold.",
    )
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=REGRESSION_THRESHOLD,
        help=f"Absolute drop that counts as a regression (default {REGRESSION_THRESHOLD}).",
    )
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

    if args.emit_json:
        payload = to_report(args.track, results)
        args.emit_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {args.emit_json}")  # noqa: T201

    regressions: list[Regression] = []
    if args.baseline:
        if not args.baseline.exists():
            print(f"ERROR: baseline {args.baseline} not found", file=sys.stderr)  # noqa: T201
            return 2
        current = to_report(args.track, results)
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        regressions = diff_against_baseline(current, baseline, args.regression_threshold)
        if regressions:
            print("\nREGRESSIONS:")  # noqa: T201
            print(render_regressions(regressions))  # noqa: T201

    failures = [k for k, v in aggregates.items() if v < THRESHOLDS.get(k, 0.0)]
    if failures:
        print(f"\nFAIL: below threshold on {failures}", file=sys.stderr)  # noqa: T201
        return 1
    if regressions:
        print(f"\nFAIL: {len(regressions)} regression(s) vs baseline", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
