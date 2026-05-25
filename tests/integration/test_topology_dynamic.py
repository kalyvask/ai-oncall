"""Dynamic topology from spans, plus the live-then-yaml fallback in builder.

Three layers:
  1. `from_spans` is a pure function — tested with hand-rolled span lists.
  2. `query_spans` on SqliteStore round-trips written spans.
  3. `build()` returns live topology when spans exist, falls back to
     topology.yaml when the store is empty, and tolerates LiveStore raising
     NotImplementedError on query_spans.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_oncall.models import TelemetryRecord
from ai_oncall.storage.sqlite import SqliteStore
from ai_oncall.topology.builder import build, load_from_spans, load_static
from ai_oncall.topology.from_spans import from_spans

T0 = datetime(2026, 4, 25, 2, 0, tzinfo=timezone.utc)
TENANT = "alpha"


def _span(
    *,
    service: str,
    span_id: str,
    parent: str | None = None,
    duration_ms: float = 100.0,
    status: str = "ok",
    when: datetime = T0,
) -> TelemetryRecord:
    return TelemetryRecord(
        tenant_id=TENANT,
        kind="trace",
        service=service,
        timestamp=when,
        trace_id="t1",
        span_id=span_id,
        parent_span_id=parent,
        name=f"{service}.handler",
        duration_ms=duration_ms,
        status=status,  # type: ignore[arg-type]
    )


# --- 1. from_spans (pure) ---------------------------------------------------


def test_from_spans_infers_edges_across_service_boundaries() -> None:
    spans = [
        _span(service="checkout", span_id="A"),
        _span(service="cart", span_id="B", parent="A"),
        _span(service="payment", span_id="C", parent="A"),
        _span(service="cart", span_id="D", parent="A"),  # second checkout->cart call
    ]
    snap = from_spans(TENANT, spans, captured_at=T0, window_minutes=10)
    assert {n.service for n in snap.nodes} == {"checkout", "cart", "payment"}
    edges = {(e.from_, e.to): e for e in snap.edges}
    assert ("checkout", "cart") in edges
    assert ("checkout", "payment") in edges
    assert edges[("checkout", "cart")].calls_per_min == pytest.approx(2 / 10)


def test_from_spans_marks_node_error_when_any_span_errors() -> None:
    spans = [
        _span(service="payment", span_id="A", status="ok"),
        _span(service="payment", span_id="B", parent="A", status="error"),
    ]
    snap = from_spans(TENANT, spans, captured_at=T0)
    payment = next(n for n in snap.nodes if n.service == "payment")
    assert payment.status == "error"


def test_from_spans_computes_error_rate_and_p99() -> None:
    spans = [
        _span(service="checkout", span_id="root"),
        _span(service="payment", span_id="a", parent="root", duration_ms=50, status="ok"),
        _span(service="payment", span_id="b", parent="root", duration_ms=80, status="ok"),
        _span(service="payment", span_id="c", parent="root", duration_ms=900, status="error"),
    ]
    snap = from_spans(TENANT, spans, captured_at=T0)
    edge = next(e for e in snap.edges if e.from_ == "checkout" and e.to == "payment")
    assert edge.error_rate == pytest.approx(1 / 3)
    assert edge.p99_ms == 900.0


def test_from_spans_skips_intra_service_parent_child() -> None:
    spans = [
        _span(service="checkout", span_id="A"),
        _span(service="checkout", span_id="A2", parent="A"),  # same-service nested span
    ]
    snap = from_spans(TENANT, spans, captured_at=T0)
    assert len(snap.edges) == 0


def test_from_spans_empty_input_yields_empty_snapshot() -> None:
    snap = from_spans(TENANT, [], captured_at=T0)
    assert snap.nodes == []
    assert snap.edges == []


# --- 2. query_spans round-trip ---------------------------------------------


def test_sqlite_query_spans_round_trips(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    spans = [
        _span(service="checkout", span_id="A"),
        _span(service="cart", span_id="B", parent="A"),
    ]
    store.write_records(TENANT, spans)
    fetched = store.query_spans(TENANT, T0 - timedelta(minutes=1))
    assert len(fetched) == 2
    assert {s.span_id for s in fetched} == {"A", "B"}


def test_query_spans_filters_by_tenant(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    store.write_records("alpha", [_span(service="x", span_id="A")])
    bravo_spans = [
        TelemetryRecord(
            tenant_id="bravo",
            kind="trace",
            service="x",
            timestamp=T0,
            span_id="B",
            trace_id="t1",
            duration_ms=10.0,
            status="ok",
        )
    ]
    store.write_records("bravo", bravo_spans)
    alpha_only = store.query_spans("alpha", T0 - timedelta(minutes=5))
    assert {s.span_id for s in alpha_only} == {"A"}


# --- 3. builder live-or-fallback -------------------------------------------


def test_load_from_spans_via_store(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    now = datetime.now(timezone.utc)
    spans = [
        _span(service="checkout", span_id="A", when=now),
        _span(service="cart", span_id="B", parent="A", when=now),
    ]
    store.write_records(TENANT, spans)
    snap = load_from_spans(TENANT, store, window_minutes=10, now=now)
    assert {n.service for n in snap.nodes} == {"checkout", "cart"}


def test_build_falls_back_to_yaml_when_store_empty(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    # No spans written; the fallback should load topology.yaml at repo root.
    snap = build(TENANT, store)
    assert snap.nodes, "expected fallback yaml topology to populate nodes"
    services = {n.service for n in snap.nodes}
    assert "checkout" in services


def test_build_prefers_live_when_spans_exist(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    now = datetime.now(timezone.utc)
    store.write_records(
        TENANT,
        [
            _span(service="alpha-only-svc", span_id="A", when=now),
        ],
    )
    snap = build(TENANT, store, now=now)
    services = {n.service for n in snap.nodes}
    assert services == {"alpha-only-svc"}


def test_build_handles_store_without_query_spans() -> None:
    """LiveStore raises NotImplementedError on query_spans; build catches and
    falls back to yaml."""
    from ai_oncall.storage.live import LiveStore

    class _NoSpansLive(LiveStore):
        def __init__(self) -> None:  # bypass parent constructor
            pass

    snap = build(TENANT, _NoSpansLive())
    assert snap.nodes  # yaml fallback


def test_load_static_unchanged() -> None:
    snap = load_static(TENANT)
    assert snap.tenant_id == TENANT
    assert snap.nodes
