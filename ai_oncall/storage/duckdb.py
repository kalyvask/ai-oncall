"""DuckDB driver. Default for single-node prod. Supports real percentiles via
`quantile()`. Same wire surface as SqliteStore.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

import duckdb

from ai_oncall.models import ChangeEvent, TelemetryRecord
from ai_oncall.storage import _sql
from ai_oncall.storage.base import TelemetryStore


class DuckDbStore(TelemetryStore):
    def __init__(self, path: str = "data/telemetry.duckdb") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = duckdb.connect(path)
        self._conn.execute(_sql.DDL_TELEMETRY)
        self._conn.execute(_sql.DDL_CHANGES)
        for ddl in _sql.DDL_INDEXES:
            self._conn.execute(ddl)

    def write_records(self, tenant_id: str, records: list[TelemetryRecord]) -> None:
        rows = []
        for r in records:
            if r.tenant_id != tenant_id:
                raise ValueError(f"record tenant_id={r.tenant_id!r} does not match scope {tenant_id!r}")
            rows.append((
                r.tenant_id, r.kind, r.service, r.timestamp.isoformat(),
                r.trace_id, r.span_id, r.parent_span_id, r.name,
                r.duration_ms, r.status, r.metric_value, r.metric_unit,
                r.severity, r.body, json.dumps(r.attributes),
            ))
        self._conn.executemany(_sql.INSERT_TELEMETRY, rows)

    def query_metric(
        self,
        tenant_id: str,
        service: str,
        metric: str,
        since: datetime,
        agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"],
    ) -> list[tuple[datetime, float]]:
        expr = _sql.METRIC_AGG_SQL[agg]
        sql = (
            f"SELECT timestamp, {expr} AS value FROM telemetry "
            f"WHERE tenant_id = ? AND service = ? AND kind = 'metric' "
            f"AND name = ? AND timestamp >= ? GROUP BY timestamp ORDER BY timestamp LIMIT 60"
        )
        rows = self._conn.execute(sql, [tenant_id, service, metric, since.isoformat()]).fetchall()
        return [(datetime.fromisoformat(ts), float(v)) for ts, v in rows]

    def query_logs(
        self,
        tenant_id: str,
        service: str,
        since: datetime,
        regex: str,
        limit: int = 50,
    ) -> list[TelemetryRecord]:
        # DuckDB has regexp_matches; use it.
        sql = (
            "SELECT tenant_id, kind, service, timestamp, trace_id, span_id, parent_span_id, "
            "name, duration_ms, status, metric_value, metric_unit, severity, body, attributes_json "
            "FROM telemetry WHERE tenant_id = ? AND service = ? AND kind = 'log' "
            "AND timestamp >= ? AND regexp_matches(body, ?) ORDER BY timestamp DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, [tenant_id, service, since.isoformat(), regex, limit]).fetchall()
        out: list[TelemetryRecord] = []
        for row in rows:
            out.append(TelemetryRecord.model_validate({
                "tenant_id": row[0], "kind": row[1], "service": row[2],
                "timestamp": row[3], "trace_id": row[4], "span_id": row[5],
                "parent_span_id": row[6], "name": row[7], "duration_ms": row[8],
                "status": row[9], "metric_value": row[10], "metric_unit": row[11],
                "severity": row[12], "body": row[13],
                "attributes": json.loads(row[14]) if row[14] else {},
            }))
        return out

    def recent_deploys(
        self, tenant_id: str, service: str, since: datetime
    ) -> list[ChangeEvent]:
        rows = self._conn.execute(_sql.QUERY_RECENT_DEPLOYS, [tenant_id, service, since.isoformat()]).fetchall()
        out: list[ChangeEvent] = []
        for row in rows:
            out.append(ChangeEvent.model_validate({
                "tenant_id": row[0], "event_id": row[1], "service": row[2],
                "kind": row[3], "timestamp": row[4], "actor": row[5],
                "title": row[6], "url": row[7], "sha": row[8],
                "patch_excerpt": row[9],
                "files_changed": json.loads(row[10]) if row[10] else [],
            }))
        return out


