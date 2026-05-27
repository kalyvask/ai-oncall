"""Three-track eval runner. BRIEF.md §7.

Tracks:
  * synthetic  — hand-crafted fault families under ``evals/cases/``.
  * rcaeval    — RCAEval RE3-OB benchmark; ``--data-dir`` required.
  * openrca    — OpenRCA Bank benchmark;   ``--data-dir`` required.

All three tracks run in replay mode today: predicted report == expected
report, so the harness exercises the schema, scoring, and aggregation paths
end-to-end with deterministic scores. When the agent prediction step is
wired in (BRIEF.md §11 step 6), only ``_predict`` changes; the scoring and
aggregation paths stay identical.

CI fail-fast: any track regression > 5 percentage points absolute fails the
run. The same threshold is used by ``--baseline`` to compare a current run
against a previous JSON snapshot and surface per-metric per-family drops.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ai_oncall.models import Alert, RcaReport
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


def _predict(_alert: Alert, expected: RcaReport) -> RcaReport:
    """Replay mode: the predicted report is the expected fixture.

    Swap this for ``ai_oncall.agent.run.run_rca(alert, store, llm)`` once a
    track is ready to exercise the real agent (BRIEF.md §11 step 6). The
    scoring path downstream stays unchanged.
    """
    return expected


def _load_case(path: Path) -> CaseResult:
    with path.open(encoding="utf-8") as f:
        case = json.load(f)
    expected = RcaReport.model_validate_json(
        (REPO / case["expected_fixture"]).read_text(encoding="utf-8")
    )
    predicted = _predict(expected.alert, expected)
    return CaseResult(
        case_id=case["case_id"],
        family=case["family"],
        difficulty=case["difficulty"],
        metrics=score_all(predicted, expected),
    )


def run(cases_dir: Path = CASES_DIR) -> list[CaseResult]:
    return [_load_case(p) for p in sorted(cases_dir.glob("*.json"))]


def _run_benchmark_track(
    track: str,
    loader: Callable[[Path], Iterator[tuple[Alert, RcaReport]]],
    data_dir: Path,
) -> list[CaseResult]:
    """Score every ``(Alert, RcaReport)`` pair yielded by ``loader``.

    Replay mode today (predicted == expected) so the path is exercised end-
    to-end. Family is read from the expected report's ``alert.labels.family``
    when present, otherwise pinned to the track name; difficulty is pinned
    to ``"real"``.
    """
    results: list[CaseResult] = []
    for alert, expected in loader(data_dir):
        predicted = _predict(alert, expected)
        family = expected.alert.labels.get("family", track)
        case_id = expected.alert.labels.get(
            "scenario" if track == "rcaeval" else "incident",
            expected.report_id,
        )
        results.append(
            CaseResult(
                case_id=str(case_id),
                family=str(family),
                difficulty="real",
                metrics=score_all(predicted, expected),
            )
        )
    return results


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
        lines.append(f"{r.scope}\t{r.metric}\t{r.baseline:.3f}\t{r.current:.3f}\t{r.delta:+.3f}")
    return "\n".join(lines)


def _load_synthetic_cases() -> list[tuple[str, str, Alert, RcaReport]]:
    """Iterate (case_id, family, alert, expected) for the synthetic track."""
    out: list[tuple[str, str, Alert, RcaReport]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as f:
            case = json.load(f)
        expected = RcaReport.model_validate_json(
            (REPO / case["expected_fixture"]).read_text(encoding="utf-8")
        )
        out.append((case["case_id"], case["family"], expected.alert, expected))
    return out


def _run_model_compare(spec: str, output_path: Path | None) -> int:
    """Run the synthetic track once per model alias and print a Markdown table."""
    from evals.model_compare import render_markdown, run_model

    aliases = [a.strip() for a in spec.split(",") if a.strip()]
    if not aliases:
        print("ERROR: --model-compare expects a comma-separated list of aliases", file=sys.stderr)  # noqa: T201
        return 2

    cases = _load_synthetic_cases()
    runs = []
    for alias in aliases:
        print(f"running {alias} over {len(cases)} synthetic case(s)…", file=sys.stderr)  # noqa: T201
        runs.append(run_model(alias, cases))

    table = render_markdown(runs)
    print(table)  # noqa: T201

    if output_path:
        output_path.write_text(table + "\n", encoding="utf-8")
        print(f"\nWrote {output_path}", file=sys.stderr)  # noqa: T201

    return 0


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
    parser.add_argument(
        "--model-compare",
        type=str,
        default=None,
        help=(
            "Comma-separated model aliases to compare on the synthetic track using "
            "live single-shot predictions (e.g. claude-haiku,claude-sonnet,claude-opus). "
            "Requires ANTHROPIC_API_KEY. Emits a Markdown comparison table to stdout."
        ),
    )
    parser.add_argument(
        "--model-compare-output",
        type=Path,
        default=None,
        help="Write the Markdown comparison table to this file in addition to stdout.",
    )
    args = parser.parse_args(argv)

    if args.model_compare:
        return _run_model_compare(args.model_compare, args.model_compare_output)

    if args.track == "synthetic":
        results = run()
    elif args.track == "rcaeval":
        from evals.rcaeval_loader import load_cases as load_rcaeval

        if not args.data_dir:
            print("ERROR: --data-dir required for --track=rcaeval", file=sys.stderr)  # noqa: T201
            return 2
        try:
            results = _run_benchmark_track("rcaeval", load_rcaeval, args.data_dir)
        except (FileNotFoundError, NotADirectoryError) as exc:
            print(f"ERROR: rcaeval data dir: {exc}", file=sys.stderr)  # noqa: T201
            return 2
    else:  # openrca
        from evals.openrca_loader import load_cases as load_openrca

        if not args.data_dir:
            print("ERROR: --data-dir required for --track=openrca", file=sys.stderr)  # noqa: T201
            return 2
        try:
            results = _run_benchmark_track("openrca", load_openrca, args.data_dir)
        except (FileNotFoundError, NotADirectoryError) as exc:
            print(f"ERROR: openrca data dir: {exc}", file=sys.stderr)  # noqa: T201
            return 2

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
