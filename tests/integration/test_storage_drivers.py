"""Both real drivers (SQLite, DuckDB) implement the same contract.

The Snowflake stub is exercised by a separate test that asserts every method
raises NotImplementedError.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_oncall.models import ChangeEvent, TelemetryRecord
from ai_oncall.storage.duckdb import DuckDbStore
from ai_oncall.storage.snowflake import SnowflakeStore
from ai_oncall.storage.sqlite import SqliteStore

T0 = datetime(2026, 4, 25, 2, 0, tzinfo=timezone.utc)
TENANT_A, TENANT_B = "alpha", "bravo"


def _records(tenant: str) -> list[TelemetryRecord]:
    return [
        TelemetryRecord(
            tenant_id=tenant, kind="metric", service="payment", timestamp=T0,
            name="http.server.duration", metric_value=1100.0, metric_unit="ms",
        ),
        TelemetryRecord(
            tenant_id=tenant, kind="metric", service="payment", timestamp=T0,
            name="http.server.duration", metric_value=2200.0, metric_unit="ms",
        ),
        TelemetryRecord(
            tenant_id=tenant, kind="log", service="payment", timestamp=T0,
            severity="error", body="TypeError: charges.create() takes 0 positional arguments but 1 given",
        ),
        TelemetryRecord(
            tenant_id=tenant, kind="log", service="payment", timestamp=T0,
            severity="info", body="hello world",
        ),
    ]


def _change(tenant: str) -> ChangeEvent:
    return ChangeEvent(
        tenant_id=tenant, event_id="abc1234", service="payment",
        kind="pr_merged", timestamp=T0, actor="alice", title="bump stripe SDK",
    )


@pytest.fixture(params=[SqliteStore, DuckDbStore])
def store(request, tmp_path):
    cls = request.param
    if cls is SqliteStore:
        return cls(path=str(tmp_path / "app.sqlite"))
    return cls(path=str(tmp_path / "telemetry.duckdb"))


def test_write_and_read_back_isolated_by_tenant(store) -> None:
    store.write_records(TENANT_A, _records(TENANT_A))
    store.write_records(TENANT_B, _records(TENANT_B))

    a_logs = store.query_logs(TENANT_A, "payment", T0, "TypeError", limit=10)
    b_logs = store.query_logs(TENANT_B, "payment", T0, "TypeError", limit=10)
    assert len(a_logs) == 1
    assert len(b_logs) == 1
    assert a_logs[0].tenant_id == TENANT_A
    assert b_logs[0].tenant_id == TENANT_B


def test_query_metric_returns_aggregated_points(store) -> None:
    store.write_records(TENANT_A, _records(TENANT_A))
    points = store.query_metric(TENANT_A, "payment", "http.server.duration", T0, "p99")
    assert len(points) >= 1
    assert points[0][1] >= 1100.0


def test_write_records_rejects_cross_tenant(store) -> None:
    with pytest.raises(ValueError, match="does not match scope"):
        store.write_records(TENANT_A, _records(TENANT_B))


def test_snowflake_stub_raises_not_implemented() -> None:
    s = SnowflakeStore()
    with pytest.raises(NotImplementedError, match="Snowflake driver is stubbed"):
        s.write_records("acme", [])
    with pytest.raises(NotImplementedError):
        s.query_metric("acme", "x", "y", T0, "p99")
    with pytest.raises(NotImplementedError):
        s.query_logs("acme", "x", T0, ".*")
    with pytest.raises(NotImplementedError):
        s.recent_deploys("acme", "x", T0)
