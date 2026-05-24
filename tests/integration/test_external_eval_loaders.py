"""Loader and harness tests for the RCAEval RE3-OB and OpenRCA Bank tracks.

Synthetic fixtures match the documented on-disk layout in each loader's
docstring; no upstream code is vendored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.harness import main as harness_main
from evals.openrca_loader import load_cases as openrca_load
from evals.rcaeval_loader import load_cases as rcaeval_load


# --- RCAEval ----------------------------------------------------------------


def _write_rcaeval_scenario(root: Path, name: str, gt: dict[str, Any]) -> None:
    scenario = root / name
    scenario.mkdir(parents=True, exist_ok=True)
    (scenario / "gt.json").write_text(json.dumps(gt), encoding="utf-8")


def test_rcaeval_loader_yields_alert_and_expected(tmp_path: Path) -> None:
    _write_rcaeval_scenario(
        tmp_path,
        "cpu_contention",
        {
            "service": "payment",
            "reason": "noisy neighbor pegged the host cpu",
            "fired_at": "2026-04-25T03:14:00Z",
            "action": "scale payment to 4 replicas",
        },
    )
    _write_rcaeval_scenario(
        tmp_path,
        "memory_leak",
        {"service": "cart", "reason": "leak on /api/cart/add"},
    )

    pairs = list(rcaeval_load(tmp_path))
    assert len(pairs) == 2
    alert, expected = pairs[0]
    assert alert.tenant_id == "rcaeval"
    assert alert.service == "payment"
    assert alert.labels["scenario"] == "cpu_contention"
    assert expected.hypotheses[0].root_cause_service == "payment"
    assert "noisy neighbor" in expected.hypotheses[0].reasoning


def test_rcaeval_loader_skips_malformed_scenarios(tmp_path: Path, caplog) -> None:
    _write_rcaeval_scenario(tmp_path, "good", {"service": "checkout", "reason": "ok"})
    (tmp_path / "no_gt").mkdir()
    bad = tmp_path / "bad_json"
    bad.mkdir()
    (bad / "gt.json").write_text("{ not json", encoding="utf-8")
    missing_service = tmp_path / "missing_service"
    missing_service.mkdir()
    (missing_service / "gt.json").write_text(json.dumps({"reason": "no svc"}), encoding="utf-8")

    pairs = list(rcaeval_load(tmp_path))
    assert len(pairs) == 1
    assert pairs[0][0].labels["scenario"] == "good"


def test_rcaeval_loader_raises_on_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(rcaeval_load(tmp_path / "does_not_exist"))


# --- OpenRCA ----------------------------------------------------------------


def _write_openrca_incident(root: Path, incident_id: str, payload: dict[str, Any]) -> None:
    incidents = root / "incidents"
    incidents.mkdir(parents=True, exist_ok=True)
    (incidents / f"{incident_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_openrca_loader_yields_alert_and_expected(tmp_path: Path) -> None:
    _write_openrca_incident(
        tmp_path,
        "openrca-2024-0001",
        {
            "incident_id": "openrca-2024-0001",
            "service": "payment",
            "alerting_service": "checkout",
            "narrative": "stripe sdk bump regressed signature; checkout cascaded.",
            "fired_at": "2024-09-12T15:02:11Z",
            "action": "rolled back commit abc1234",
            "alert_title": "checkout p99 SLO breach",
        },
    )
    _write_openrca_incident(
        tmp_path,
        "noise-row",
        {"service": "cart", "narrative": "not actually an incident", "noise": True},
    )

    pairs = list(openrca_load(tmp_path))
    assert len(pairs) == 1
    alert, expected = pairs[0]
    assert alert.tenant_id == "openrca"
    assert alert.service == "checkout"  # alert fires on the symptom
    assert alert.expected_focus_service == "payment"  # ground-truth root cause
    assert expected.hypotheses[0].root_cause_service == "payment"
    assert "stripe sdk" in expected.hypotheses[0].reasoning


def test_openrca_loader_accepts_flat_layout(tmp_path: Path) -> None:
    """Falls back to <data_dir>/*.json when there is no incidents/ subdir."""
    (tmp_path / "openrca-2024-0042.json").write_text(
        json.dumps({"service": "auth", "narrative": "session token TTL drift"}),
        encoding="utf-8",
    )
    pairs = list(openrca_load(tmp_path))
    assert len(pairs) == 1
    assert pairs[0][1].hypotheses[0].root_cause_service == "auth"


def test_openrca_loader_raises_on_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(openrca_load(tmp_path / "does_not_exist"))


# --- harness wiring ---------------------------------------------------------


def test_harness_rcaeval_track_scores_loaded_cases(tmp_path: Path, capsys) -> None:
    _write_rcaeval_scenario(
        tmp_path,
        "cpu_contention",
        {"service": "payment", "reason": "cpu pegged"},
    )
    rc = harness_main(["--track", "rcaeval", "--data-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cpu_contention" in out
    # Replay mode -> perfect component_match.
    assert "1.00" in out


def test_harness_openrca_track_scores_loaded_cases(tmp_path: Path, capsys) -> None:
    _write_openrca_incident(
        tmp_path,
        "openrca-2024-0001",
        {"service": "payment", "narrative": "stripe sdk bump"},
    )
    rc = harness_main(["--track", "openrca", "--data-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "openrca-2024-0001" in out


def test_harness_rejects_missing_data_dir(capsys) -> None:
    rc = harness_main(["--track", "rcaeval"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "data-dir required" in err


def test_harness_reports_data_dir_not_found(tmp_path: Path, capsys) -> None:
    rc = harness_main(
        ["--track", "openrca", "--data-dir", str(tmp_path / "does_not_exist")]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "openrca data dir" in err
