"""Loki HTTP API client. Implements the logs half of the live store.

Uses `/loki/api/v1/query_range`. Service is identified by a label (default
`service`, override with `AI_ONCALL_LOKI_SERVICE_LABEL`). Regex is matched
via LogQL's `|~` operator. Loki returns timestamps as nanosecond strings;
parse to datetime here.

Auth: bearer token via `AI_ONCALL_LOKI_TOKEN`. Cap is 50 lines per call
(matches BRIEF.md §6).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ai_oncall.models import TelemetryRecord

LINE_LIMIT = 50


class LokiClient:
    def __init__(
        self,
        base_url: str,
        *,
        service_label: str = "service",
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_label = service_label
        self.token = token
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def query_logs(
        self,
        tenant_id: str,
        service: str,
        since: datetime,
        regex: str,
        limit: int = 50,
    ) -> list[TelemetryRecord]:
        capped = min(limit, LINE_LIMIT)
        end = datetime.now(timezone.utc)
        start = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        params = {
            "query": f'{{{self.service_label}="{service}"}} |~ "{_escape_logql(regex)}"',
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": capped,
            "direction": "backward",
        }
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        resp = self._client.get(
            f"{self.base_url}/loki/api/v1/query_range", params=params, headers=headers
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            return []
        result = payload.get("data", {}).get("result", [])
        records: list[TelemetryRecord] = []
        for stream in result:
            stream_labels = stream.get("stream", {})
            severity = stream_labels.get("severity") or stream_labels.get("level")
            for ts_ns, line in stream.get("values", []):
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
                records.append(
                    TelemetryRecord(
                        tenant_id=tenant_id,
                        kind="log",
                        service=service,
                        timestamp=ts,
                        severity=_normalize_severity(severity),
                        body=line,
                    )
                )
                if len(records) >= capped:
                    return records
        return records


def _escape_logql(regex: str) -> str:
    # LogQL string escaping: backslash and double-quote.
    return regex.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_severity(value: str | None) -> str | None:
    if not value:
        return None
    v = value.lower()
    mapping = {
        "trace": "trace", "debug": "debug", "info": "info",
        "warn": "warn", "warning": "warn", "error": "error",
        "err": "error", "fatal": "fatal", "crit": "fatal", "critical": "fatal",
    }
    return mapping.get(v)
