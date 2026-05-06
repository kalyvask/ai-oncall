"""Shared schema + query bodies for SQLite and DuckDB drivers.

Multi-tenancy is enforced at the query layer: every WHERE clause must filter by
tenant_id. The drivers are thin adapters that bind connections; the SQL itself
is authored once here.
"""

from __future__ import annotations

DDL_TELEMETRY = """
CREATE TABLE IF NOT EXISTS telemetry (
  tenant_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  service TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  trace_id TEXT,
  span_id TEXT,
  parent_span_id TEXT,
  name TEXT,
  duration_ms REAL,
  status TEXT,
  metric_value REAL,
  metric_unit TEXT,
  severity TEXT,
  body TEXT,
  attributes_json TEXT
)
"""

DDL_CHANGES = """
CREATE TABLE IF NOT EXISTS change_events (
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  service TEXT NOT NULL,
  kind TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  actor TEXT NOT NULL,
  title TEXT,
  url TEXT,
  sha TEXT,
  patch_excerpt TEXT,
  files_changed_json TEXT,
  PRIMARY KEY (tenant_id, event_id)
)
"""

DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_telemetry_tenant_service_kind_ts ON telemetry (tenant_id, service, kind, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_changes_tenant_service_ts ON change_events (tenant_id, service, timestamp)",
]

INSERT_TELEMETRY = """
INSERT INTO telemetry (
  tenant_id, kind, service, timestamp, trace_id, span_id, parent_span_id,
  name, duration_ms, status, metric_value, metric_unit, severity, body,
  attributes_json
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

# Aggregated metric query. The agg parameter is interpolated at query-build
# time into a SQL function name from a fixed allow-list — never user input.
METRIC_AGG_SQL = {
    "p50": "quantile(metric_value, 0.50)",
    "p95": "quantile(metric_value, 0.95)",
    "p99": "quantile(metric_value, 0.99)",
    "avg": "avg(metric_value)",
    "sum": "sum(metric_value)",
    # rate = events/sec over the bucket; SQLite cannot quantile so this is a
    # straight count; drivers fall back to count()/window-seconds.
    "rate": "count(*)",
}


def query_metric_sql(agg: str, bucket_seconds: int = 60) -> str:
    if agg not in METRIC_AGG_SQL:
        raise ValueError(f"unsupported agg: {agg}")
    expr = METRIC_AGG_SQL[agg]
    return f"""
        SELECT timestamp, {expr} AS value
        FROM telemetry
        WHERE tenant_id = ? AND service = ? AND kind = 'metric'
          AND name = ? AND timestamp >= ?
        GROUP BY timestamp
        ORDER BY timestamp
        LIMIT 60
    """  # bucket_seconds reserved for follow-up time-bucketing pass


QUERY_LOGS = """
SELECT tenant_id, kind, service, timestamp, trace_id, span_id, parent_span_id,
       name, duration_ms, status, metric_value, metric_unit, severity, body,
       attributes_json
FROM telemetry
WHERE tenant_id = ? AND service = ? AND kind = 'log' AND timestamp >= ?
  AND body REGEXP ?
ORDER BY timestamp DESC
LIMIT ?
"""

QUERY_LOGS_LIKE = """
SELECT tenant_id, kind, service, timestamp, trace_id, span_id, parent_span_id,
       name, duration_ms, status, metric_value, metric_unit, severity, body,
       attributes_json
FROM telemetry
WHERE tenant_id = ? AND service = ? AND kind = 'log' AND timestamp >= ?
  AND body LIKE ?
ORDER BY timestamp DESC
LIMIT ?
"""

QUERY_RECENT_DEPLOYS = """
SELECT tenant_id, event_id, service, kind, timestamp, actor, title, url, sha,
       patch_excerpt, files_changed_json
FROM change_events
WHERE tenant_id = ? AND service = ? AND timestamp >= ?
ORDER BY timestamp DESC
LIMIT 25
"""

QUERY_SPANS = """
SELECT tenant_id, kind, service, timestamp, trace_id, span_id, parent_span_id,
       name, duration_ms, status, metric_value, metric_unit, severity, body,
       attributes_json
FROM telemetry
WHERE tenant_id = ? AND kind = 'trace' AND timestamp >= ?
ORDER BY timestamp DESC
LIMIT ?
"""
