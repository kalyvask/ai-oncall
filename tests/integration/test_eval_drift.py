"""Eval drift detection. Compares current run aggregates vs a baseline
snapshot and surfaces per-metric per-family drops.

Tests cover the pure helpers (`aggregate_by_family`, `to_report`,
`diff_against_baseline`) and one CLI roundtrip via `main()`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.harness import (
    REGRESSION_THRESHOLD,
    CaseResult,
    Regression,
    aggregate_by_family,
    diff_against_baseline,
    main,
    to_report,
)


def _case(case_id: str, family: str, **metrics: float) -> CaseResult:
    return CaseResult(
        case_id=case_id, family=family, difficulty="easy",
        metrics={
            "component_match": metrics.get("component_match", 1.0),
            "reason_cosine": metrics.get("reason_cosine", 1.0),
            "trajectory_score": metrics.get("trajectory_score", 2.0),
            "escalation_precision": metrics.get("escalation_precision", 1.0),
        },
    )


# --- aggregation by family -------------------------------------------------


def test_aggregate_by_family_groups_metrics() -> None:
    rows = [
        _case("a", "deploy_regression", component_match=1.0),
        _case("b", "deploy_regression", component_match=0.5),
        _case("c", "noisy_neighbor", component_match=0.0),
    ]
    by_family = aggregate_by_family(rows)
    assert set(by_family) == {"deploy_regression", "noisy_neighbor"}
    assert by_family["deploy_regression"]["component_match"] == pytest.approx(0.75)
    assert by_family["noisy_neighbor"]["component_match"] == 0.0


def test_to_report_emits_global_and_per_family() -> None:
    rows = [_case("a", "x"), _case("b", "y")]
    report = to_report("synthetic", rows)
    assert report["track"] == "synthetic"
    assert "captured_at" in report
    assert len(report["results"]) == 2
    assert "component_match" in report["aggregates_global"]
    assert set(report["aggregates_by_family"]) == {"x", "y"}


# --- diff_against_baseline -------------------------------------------------


def test_no_regression_when_runs_are_identical() -> None:
    rows = [_case("a", "x"), _case("b", "y")]
    snapshot = to_report("synthetic", rows)
    assert diff_against_baseline(snapshot, snapshot) == []


def test_regression_detected_in_global_metric() -> None:
    base_rows = [_case("a", "x", component_match=1.0)]
    cur_rows = [_case("a", "x", component_match=0.5)]  # 50pp drop
    base = to_report("synthetic", base_rows)
    cur = to_report("synthetic", cur_rows)
    regressions = diff_against_baseline(cur, base)
    scopes = {(r.scope, r.metric) for r in regressions}
    assert ("global", "component_match") in scopes
    assert ("x", "component_match") in scopes


def test_regression_detected_per_family_only() -> None:
    """Even if global is fine, a per-family drop should still be flagged."""
    base = to_report("synthetic", [
        _case("a", "good", component_match=1.0),
        _case("b", "bad", component_match=1.0),
    ])
    cur = to_report("synthetic", [
        _case("a", "good", component_match=1.0),
        _case("b", "bad", component_match=0.5),  # only "bad" family regressed
    ])
    regressions = diff_against_baseline(cur, base)
    scopes = {(r.scope, r.metric) for r in regressions}
    assert ("bad", "component_match") in scopes
    assert ("good", "component_match") not in scopes


def test_drop_under_threshold_is_not_a_regression() -> None:
    base = to_report("synthetic", [_case("a", "x", component_match=1.0)])
    cur = to_report("synthetic", [_case("a", "x", component_match=1.0 - REGRESSION_THRESHOLD + 0.001)])
    assert diff_against_baseline(cur, base) == []


def test_drop_at_threshold_boundary_is_a_regression() -> None:
    base = to_report("synthetic", [_case("a", "x", component_match=1.0)])
    # exactly threshold + epsilon below
    cur = to_report("synthetic", [_case("a", "x", component_match=1.0 - REGRESSION_THRESHOLD - 0.001)])
    regressions = diff_against_baseline(cur, base)
    assert any(r.scope == "global" and r.metric == "component_match" for r in regressions)


def test_improvements_are_not_flagged() -> None:
    base = to_report("synthetic", [_case("a", "x", component_match=0.5)])
    cur = to_report("synthetic", [_case("a", "x", component_match=1.0)])
    assert diff_against_baseline(cur, base) == []


def test_new_family_in_current_does_not_trigger_regression() -> None:
    base = to_report("synthetic", [_case("a", "x")])
    cur = to_report("synthetic", [_case("a", "x"), _case("b", "y")])
    assert diff_against_baseline(cur, base) == []  # "y" has no baseline to compare


def test_returns_typed_regression_records() -> None:
    base = to_report("synthetic", [_case("a", "x", component_match=1.0)])
    cur = to_report("synthetic", [_case("a", "x", component_match=0.4)])
    regressions = diff_against_baseline(cur, base)
    assert all(isinstance(r, Regression) for r in regressions)
    a_global = next(r for r in regressions if r.scope == "global")
    assert a_global.baseline == pytest.approx(1.0)
    assert a_global.current == pytest.approx(0.4)
    assert a_global.delta == pytest.approx(-0.6)


# --- CLI roundtrip --------------------------------------------------------


def test_cli_emit_json_writes_complete_report(tmp_path: Path) -> None:
    out = tmp_path / "run.json"
    rc = main(["--track", "synthetic", "--emit-json", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["track"] == "synthetic"
    assert payload["results"]
    assert payload["aggregates_global"]
    assert payload["aggregates_by_family"]


def test_cli_baseline_passes_when_identical(tmp_path: Path) -> None:
    out = tmp_path / "run.json"
    assert main(["--track", "synthetic", "--emit-json", str(out)]) == 0
    # Use the same JSON as the baseline. Since results are deterministic
    # in replay mode, the diff should be empty.
    rc = main(["--track", "synthetic", "--baseline", str(out)])
    assert rc == 0


def test_cli_baseline_fails_when_artificially_regressed(tmp_path: Path) -> None:
    out = tmp_path / "run.json"
    assert main(["--track", "synthetic", "--emit-json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    # Inflate the baseline so the current run looks like a regression.
    inflated = {**payload}
    inflated["aggregates_global"] = {k: 1.0 for k in payload["aggregates_global"]}
    inflated["aggregates_by_family"] = {
        family: {k: 1.0 for k in metrics}
        for family, metrics in payload["aggregates_by_family"].items()
    }
    # If current is already at 1.0 there is nothing to drop; force a delta by
    # setting baseline higher than the legal max via a hand-crafted file.
    inflated["aggregates_global"] = {
        k: v + 0.5 for k, v in payload["aggregates_global"].items()
    }
    inflated["aggregates_by_family"] = {
        family: {k: v + 0.5 for k, v in metrics.items()}
        for family, metrics in payload["aggregates_by_family"].items()
    }
    bumped = tmp_path / "bumped.json"
    bumped.write_text(json.dumps(inflated), encoding="utf-8")
    rc = main(["--track", "synthetic", "--baseline", str(bumped)])
    assert rc == 1


def test_cli_baseline_missing_file_returns_2(tmp_path: Path) -> None:
    rc = main(["--track", "synthetic", "--baseline", str(tmp_path / "nope.json")])
    assert rc == 2
