"""SQLite driver. Default for development. Multi-tenant filtering enforced in
every query; SQLite does not support `quantile`, so percentile aggs use
`MAX(metric_value)` as a documented approximation — promote to DuckDB for real
percentile fidelity, or to Snowflake for cross-tenant production scale.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ai_oncall.models import ChangeEvent, TelemetryRecord
from ai_oncall.storage import _sql
from ai_oncall.storage.base import TelemetryStore

# SQLite percentile fallback: pick the worst observation in the bucket.
_SQLITE_AGG = {
    **_sql.METRIC_AGG_SQL,
    "p50": "max(metric_value)",
    "p95": "max(metric_value)",
    "p99": "max(metric_value)",
}


def _regex(pattern: str, value: str | None) -> int:
    if value is None:
        return 0
    return 1 if re.search(pattern, value) else 0


class SqliteStore(TelemetryStore):
    def __init__(self, path: str = "data/app.sqlite") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.create_function("REGEXP", 2, _regex)
        cur = self._conn.cursor()
        cur.execute(_sql.DDL_TELEMETRY)
        cur.execute(_sql.DDL_CHANGES)
        for ddl in _sql.DDL_INDEXES:
            cur.execute(ddl)
        self._conn.commit()

    def write_records(self, tenant_id: str, records: list[TelemetryRecord]) -> None:
        rows = []
        for r in records:
            if r.tenant_id != tenant_id:
                raise ValueError(
                    f"record tenant_id={r.tenant_id!r} does not match scope {tenant_id!r}"
                )
            rows.append(
                (
                    r.tenant_id,
                    r.kind,
                    r.service,
                    r.timestamp.isoformat(),
                    r.trace_id,
                    r.span_id,
                    r.parent_span_id,
                    r.name,
                    r.duration_ms,
                    r.status,
                    r.metric_value,
                    r.metric_unit,
                    r.severity,
                    r.body,
                    json.dumps(r.attributes),
                )
            )
        self._conn.executemany(_sql.INSERT_TELEMETRY, rows)
        self._conn.commit()

    def query_metric(
        self,
        tenant_id: str,
        service: str,
        metric: str,
        since: datetime,
        agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"],
    ) -> list[tuple[datetime, float]]:
        expr = _SQLITE_AGG[agg]
        sql = (
            f"SELECT timestamp, {expr} AS value FROM telemetry "
            f"WHERE tenant_id = ? AND service = ? AND kind = 'metric' "
            f"AND name = ? AND timestamp >= ? GROUP BY timestamp ORDER BY timestamp LIMIT 60"
        )
        rows = self._conn.execute(sql, (tenant_id, service, metric, since.isoformat())).fetchall()
        return [(datetime.fromisoformat(ts), float(v)) for ts, v in rows]

    def query_logs(
        self,
        tenant_id: str,
        service: str,
        since: datetime,
        regex: str,
        limit: int = 50,
    ) -> list[TelemetryRecord]:
        rows = self._conn.execute(
            _sql.QUERY_LOGS, (tenant_id, service, since.isoformat(), regex, limit)
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def recent_deploys(self, tenant_id: str, service: str, since: datetime) -> list[ChangeEvent]:
        rows = self._conn.execute(
            _sql.QUERY_RECENT_DEPLOYS, (tenant_id, service, since.isoformat())
        ).fetchall()
        return [_row_to_change(r) for r in rows]

    def query_spans(
        self, tenant_id: str, since: datetime, limit: int = 5000
    ) -> list[TelemetryRecord]:
        rows = self._conn.execute(
            _sql.QUERY_SPANS, (tenant_id, since.isoformat(), limit)
        ).fetchall()
        return [_row_to_record(r) for r in rows]


def _row_to_record(row: tuple[Any, ...]) -> TelemetryRecord:
    return TelemetryRecord.model_validate(
        {
            "tenant_id": row[0],
            "kind": row[1],
            "service": row[2],
            "timestamp": row[3],
            "trace_id": row[4],
            "span_id": row[5],
            "parent_span_id": row[6],
            "name": row[7],
            "duration_ms": row[8],
            "status": row[9],
            "metric_value": row[10],
            "metric_unit": row[11],
            "severity": row[12],
            "body": row[13],
            "attributes": json.loads(row[14]) if row[14] else {},
        }
    )


def _row_to_change(row: tuple[Any, ...]) -> ChangeEvent:
    return ChangeEvent.model_validate(
        {
            "tenant_id": row[0],
            "event_id": row[1],
            "service": row[2],
            "kind": row[3],
            "timestamp": row[4],
            "actor": row[5],
            "title": row[6],
            "url": row[7],
            "sha": row[8],
            "patch_excerpt": row[9],
            "files_changed": json.loads(row[10]) if row[10] else [],
        }
    )
