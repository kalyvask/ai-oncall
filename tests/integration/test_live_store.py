"""Live store: Prometheus + Loki backends, exercised via httpx.MockTransport.

No new test dependencies. The transport returns canned JSON shaped like the
real backends; we assert the wire shape is parsed correctly and that the
store delegates correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ai_oncall.storage.live import LiveStore
from ai_oncall.storage.loki import LokiClient
from ai_oncall.storage.prometheus import PrometheusClient
from ai_oncall.storage.sqlite import SqliteStore

T0 = datetime(2026, 4, 25, 2, 0, tzinfo=timezone.utc)
TENANT = "alpha"


def _prom_response(values: list[tuple[float, str]]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"service": "payment"}, "values": [[t, v] for t, v in values]}
            ],
        },
    }


def _loki_response(lines: list[tuple[int, str]], severity: str = "error") -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": "payment", "severity": severity},
                    "values": [[str(ts_ns), body] for ts_ns, body in lines],
                }
            ],
        },
    }


def _make_handler(prom_payload: dict, loki_payload: dict, capture: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.endswith("/loki/api/v1/query_range"):
            capture["loki_query"] = params.get("query")
            capture["loki_limit"] = params.get("limit")
            capture["loki_auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=loki_payload)
        if url.path.endswith("/api/v1/query_range"):
            capture["prom_query"] = params.get("query")
            capture["prom_auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=prom_payload)
        return httpx.Response(404, text=json.dumps({"error": "not found"}))

    return handler


def _build_store(prom_payload: dict, loki_payload: dict, *, prom_token: str | None = None, capture: dict | None = None) -> tuple[LiveStore, dict]:
    cap = capture if capture is not None else {}
    transport = httpx.MockTransport(_make_handler(prom_payload, loki_payload, cap))
    http = httpx.Client(transport=transport)
    prom = PrometheusClient("http://prom.example", token=prom_token, client=http)
    loki = LokiClient("http://loki.example", client=http)
    deploys = SqliteStore(path=":memory:")
    return LiveStore(prom, loki, deploys), cap


def test_query_metric_p99_uses_histogram_quantile() -> None:
    store, capture = _build_store(
        prom_payload=_prom_response([(1_700_000_000.0, "0.99"), (1_700_000_060.0, "1.10")]),
        loki_payload={"status": "success", "data": {"result": []}},
    )
    points = store.query_metric(TENANT, "payment", "http_server_duration_seconds", T0, "p99")
    assert len(points) == 2
    assert points[0][1] == pytest.approx(0.99)
    assert "histogram_quantile(0.99" in capture["prom_query"]
    assert 'service="payment"' in capture["prom_query"]


def test_query_metric_rate_uses_rate() -> None:
    store, capture = _build_store(
        prom_payload=_prom_response([(1_700_000_000.0, "12.0")]),
        loki_payload={"status": "success", "data": {"result": []}},
    )
    points = store.query_metric(TENANT, "payment", "errors_total", T0, "rate")
    assert points[0][1] == pytest.approx(12.0)
    assert capture["prom_query"].startswith("rate(errors_total")


def test_query_metric_passes_bearer_token() -> None:
    store, capture = _build_store(
        prom_payload=_prom_response([]),
        loki_payload={"status": "success", "data": {"result": []}},
        prom_token="secret",
    )
    store.query_metric(TENANT, "payment", "errors_total", T0, "rate")
    assert capture["prom_auth"] == "Bearer secret"


def test_query_metric_truncates_to_60_points() -> None:
    big = [(float(1_700_000_000 + i * 60), str(i)) for i in range(120)]
    store, _ = _build_store(
        prom_payload=_prom_response(big),
        loki_payload={"status": "success", "data": {"result": []}},
    )
    points = store.query_metric(TENANT, "payment", "errors_total", T0, "rate")
    assert len(points) == 60


def test_query_logs_parses_streams_and_caps_at_limit() -> None:
    store, capture = _build_store(
        prom_payload=_prom_response([]),
        loki_payload=_loki_response(
            [(1_700_000_000_000_000_000, "TypeError: charges.create() failed")],
            severity="error",
        ),
    )
    rows = store.query_logs(TENANT, "payment", T0, "TypeError", limit=10)
    assert len(rows) == 1
    assert rows[0].body == "TypeError: charges.create() failed"
    assert rows[0].severity == "error"
    assert rows[0].tenant_id == TENANT
    assert capture["loki_limit"] == "10"
    assert "TypeError" in capture["loki_query"]


def test_query_logs_caps_at_50_even_when_caller_asks_more() -> None:
    store, capture = _build_store(
        prom_payload=_prom_response([]),
        loki_payload=_loki_response(
            [(1_700_000_000_000_000_000 + i, f"line {i}") for i in range(80)],
            severity="warn",
        ),
    )
    rows = store.query_logs(TENANT, "payment", T0, ".*", limit=200)
    assert len(rows) == 50
    assert capture["loki_limit"] == "50"


def test_write_records_raises_on_live_store() -> None:
    store, _ = _build_store(
        prom_payload=_prom_response([]),
        loki_payload={"status": "success", "data": {"result": []}},
    )
    with pytest.raises(NotImplementedError, match="LiveStore does not accept writes"):
        store.write_records(TENANT, [])


def test_recent_deploys_delegates_to_inner_store() -> None:
    store, _ = _build_store(
        prom_payload=_prom_response([]),
        loki_payload={"status": "success", "data": {"result": []}},
    )
    out = store.recent_deploys(TENANT, "payment", T0)
    assert out == []  # delegate is empty in-memory sqlite
