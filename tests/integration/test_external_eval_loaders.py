"""Both real-data loaders are stubs today; the test pins the failure mode so
nobody silently wires a half-built loader into CI."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.openrca_loader import load_cases as openrca_load
from evals.rcaeval_loader import load_cases as rcaeval_load


def test_rcaeval_loader_raises_with_pointer() -> None:
    with pytest.raises(NotImplementedError, match="BRIEF.md"):
        list(rcaeval_load(Path(".")))


def test_openrca_loader_raises_with_pointer() -> None:
    with pytest.raises(NotImplementedError, match="BRIEF.md"):
        list(openrca_load(Path(".")))


def test_harness_skips_external_tracks_cleanly(capsys, tmp_path) -> None:
    """When the external track is requested but the loader is stubbed,
    the harness must skip with exit 0, not crash CI."""
    from evals.harness import main

    rc = main(["--track", "rcaeval", "--data-dir", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "SKIP rcaeval track" in captured.err

    rc = main(["--track", "openrca", "--data-dir", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "SKIP openrca track" in captured.err
