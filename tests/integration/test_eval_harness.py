"""Integration test for the eval harness. Replay-mode run must hit perfect
scores across all 5 synthetic cases."""

from __future__ import annotations

from evals.harness import aggregate, run


EXPECTED_FAMILIES = {
    "deploy_regression",
    "dependency_saturation",
    "config_drift",
    "slow_leak",
    "noisy_neighbor",
    "downstream_cascade",
}


def test_replay_mode_hits_perfect_scores() -> None:
    results = run()
    assert len(results) == len(EXPECTED_FAMILIES), (
        f"expected one case per fault family, got {len(results)}"
    )
    families = {r.family for r in results}
    assert families == EXPECTED_FAMILIES, families

    aggregates = aggregate(results)
    assert aggregates["component_match"] == 1.0
    assert aggregates["reason_cosine"] == 1.0
    assert aggregates["trajectory_score"] == 2.0
    assert aggregates["escalation_precision"] == 1.0


def test_thresholds_are_below_replay_score() -> None:
    """Smoke check: production thresholds must leave headroom over a perfect run."""
    from evals.harness import THRESHOLDS

    aggregates = aggregate(run())
    for metric, threshold in THRESHOLDS.items():
        assert aggregates[metric] >= threshold, f"{metric}={aggregates[metric]} < {threshold}"
