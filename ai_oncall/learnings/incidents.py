"""Persistent storage for full RCA reports.

The original v1 only persisted compact LearningRecord rows in
``learnings.jsonl``. That's enough for k-NN retrieval but leaves the agent
unable to:

- replay past incidents end-to-end (you need the original alert, not just the
  alert title),
- aggregate root causes per service for the typed memory graph,
- reconstruct the reasoning trace UI from history.

This module stores the full ``RcaReport`` (including its ``Alert`` and
``Investigation``) in a SQLite database co-located with ``data/app.sqlite``.
The schema is intentionally narrow: report_id is the key, the report blob is
the source of truth, and a small set of columns are denormalized for fast
look-ups (tenant, service, root_cause_service, created_at, root_cause_class).

Trust tier from gbrain (``local`` / ``aggregated`` / ``verified``) is stored
on the report row so cross-tenant queries can opt in / out at read time.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel

from ai_oncall.models import Alert, RcaReport


INCIDENTS_DB_PATH = Path("data/incidents.sqlite")

TrustTier = Literal["local", "aggregated", "verified"]


class IncidentRow(BaseModel):
    """A persisted RCA report plus a few denormalized fields."""

    report_id: str
    tenant_id: str
    alert_id: str
    service: str
    root_cause_service: str
    root_cause_class: str | None = None
    top_confidence: float
    abstained: bool = False
    trust_tier: TrustTier = "local"
    created_at: datetime
    report_json: str  # full RcaReport as JSON; source of truth

    def report(self) -> RcaReport:
        return RcaReport.model_validate_json(self.report_json)

    def alert(self) -> Alert:
        # The Alert is embedded inside the report; expose for replay paths.
        return RcaReport.model_validate_json(self.report_json).alert


def _resolve_db_path(db_path: Path | None) -> Path:
    """Look up the module-level path at call time so monkeypatch in tests
    actually reaches the connection. Default arguments are bound at import."""
    if db_path is not None:
        return db_path
    # Module-level lookup, not captured at import.
    import ai_oncall.learnings.incidents as _self

    return _self.INCIDENTS_DB_PATH


@contextmanager
def _conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema creation. Safe to call on every operation."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS incidents (
            report_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            alert_id TEXT NOT NULL,
            service TEXT NOT NULL,
            root_cause_service TEXT NOT NULL,
            root_cause_class TEXT,
            top_confidence REAL NOT NULL,
            abstained INTEGER DEFAULT 0,
            trust_tier TEXT NOT NULL DEFAULT 'local',
            created_at TEXT NOT NULL,
            report_json TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_tenant_service ON incidents(tenant_id, service)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_root_cause ON incidents(tenant_id, root_cause_service)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_incidents_created_at ON incidents(created_at)")

    # Typed memory graph: one row per (tenant, service, root_cause_class).
    # Bumped on every incident; latest snapshot kept for fast retrieval.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS service_root_cause_classes (
            tenant_id TEXT NOT NULL,
            service TEXT NOT NULL,
            root_cause_class TEXT NOT NULL,
            occurrences INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT NOT NULL,
            last_report_id TEXT,
            avg_confidence REAL,
            trust_tier TEXT NOT NULL DEFAULT 'local',
            PRIMARY KEY (tenant_id, service, root_cause_class)
        )
        """
    )

    # Slack thread mapping: (channel, thread_ts) -> report_id. Populated by
    # delivery/send.py on first post; read by the /webhooks/slack/event
    # handler to attribute thread replies to the right RCA without having
    # to parse the parent message.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_threads (
            channel TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            report_id TEXT NOT NULL,
            posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (channel, thread_ts)
        )
        """
    )


def save_incident(
    report: RcaReport,
    *,
    abstained: bool = False,
    trust_tier: TrustTier = "local",
    db_path: Path | None = None,
) -> IncidentRow:
    """Persist a full RCA report. Replaces existing rows by report_id."""
    if not report.hypotheses:
        raise ValueError("Cannot save report with no hypotheses")
    top = report.hypotheses[0]
    root_cause_class = _classify_root_cause(top.root_cause_service, top.recommended_action)
    row = IncidentRow(
        report_id=report.report_id,
        tenant_id=report.tenant_id,
        alert_id=report.alert.alert_id,
        service=report.alert.service,
        root_cause_service=top.root_cause_service,
        root_cause_class=root_cause_class,
        top_confidence=top.confidence,
        abstained=abstained,
        trust_tier=trust_tier,
        created_at=report.generated_at,
        report_json=report.model_dump_json(),
    )

    with _conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO incidents (
                report_id, tenant_id, alert_id, service, root_cause_service,
                root_cause_class, top_confidence, abstained, trust_tier,
                created_at, report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.report_id,
                row.tenant_id,
                row.alert_id,
                row.service,
                row.root_cause_service,
                row.root_cause_class,
                row.top_confidence,
                1 if row.abstained else 0,
                row.trust_tier,
                row.created_at.isoformat(),
                row.report_json,
            ),
        )
        # Update the typed-memory graph row.
        if row.root_cause_class is not None:
            _bump_graph_row(
                conn,
                tenant_id=row.tenant_id,
                service=row.service,
                root_cause_class=row.root_cause_class,
                last_report_id=row.report_id,
                last_seen_at=row.created_at,
                confidence=row.top_confidence,
                trust_tier=row.trust_tier,
            )

    return row


def get_incident(report_id: str, *, db_path: Path | None = None) -> Optional[IncidentRow]:
    with _conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM incidents WHERE report_id = ?", (report_id,))
        rec = cursor.fetchone()
    return _row_to_model(rec) if rec else None


def list_incidents(
    *,
    tenant_id: str,
    service: Optional[str] = None,
    root_cause_class: Optional[str] = None,
    limit: int = 25,
    db_path: Path | None = None,
) -> list[IncidentRow]:
    """Newest first. Filters compose."""
    where = ["tenant_id = ?"]
    params: list[object] = [tenant_id]
    if service:
        where.append("service = ?")
        params.append(service)
    if root_cause_class:
        where.append("root_cause_class = ?")
        params.append(root_cause_class)
    where_sql = " AND ".join(where)
    params.append(limit)

    with _conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM incidents WHERE {where_sql} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [_row_to_model(r) for r in cursor.fetchall()]


def record_thread_mapping(
    *,
    channel: str,
    thread_ts: str,
    report_id: str,
    db_path: Path | None = None,
) -> None:
    """Persist (channel, thread_ts) -> report_id so later thread events
    can recover the report. Idempotent on the primary key."""
    with _conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO slack_threads (channel, thread_ts, report_id, posted_at)
            VALUES (?, ?, ?, ?)
            """,
            (channel, thread_ts, report_id, datetime.utcnow().isoformat()),
        )


def lookup_report_id_by_thread(
    *,
    channel: str,
    thread_ts: str,
    db_path: Path | None = None,
) -> Optional[str]:
    """Reverse map for the Events API endpoint. Returns None when unknown."""
    with _conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT report_id FROM slack_threads WHERE channel = ? AND thread_ts = ?",
            (channel, thread_ts),
        )
        row = cursor.fetchone()
        return row["report_id"] if row else None


def promote_incident_tier(
    report_id: str,
    *,
    new_tier: TrustTier,
    db_path: Path | None = None,
) -> bool:
    """Promote a stored incident (and its graph row) to a higher trust tier.

    Returns True if a row was updated; False if the report_id wasn't found.
    The graph row tied to the incident's (tenant, service, root_cause_class)
    is also bumped so cross-tenant queries see the promotion.
    """
    with _conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tenant_id, service, root_cause_class FROM incidents WHERE report_id = ?",
            (report_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        cursor.execute(
            "UPDATE incidents SET trust_tier = ? WHERE report_id = ?",
            (new_tier, report_id),
        )
        if row["root_cause_class"]:
            cursor.execute(
                """
                UPDATE service_root_cause_classes
                SET trust_tier = ?
                WHERE tenant_id = ? AND service = ? AND root_cause_class = ?
                """,
                (new_tier, row["tenant_id"], row["service"], row["root_cause_class"]),
            )
    return True


def list_root_cause_classes(
    *,
    tenant_id: str,
    service: str,
    trust_tiers: tuple[TrustTier, ...] = ("local",),
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return graph rows for a (tenant, service), filtered by trust tier."""
    if not trust_tiers:
        return []
    placeholders = ",".join("?" for _ in trust_tiers)
    with _conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT root_cause_class, occurrences, last_seen_at, last_report_id,
                   avg_confidence, trust_tier
            FROM service_root_cause_classes
            WHERE tenant_id = ? AND service = ? AND trust_tier IN ({placeholders})
            ORDER BY occurrences DESC, last_seen_at DESC
            """,
            (tenant_id, service, *trust_tiers),
        )
        return [dict(r) for r in cursor.fetchall()]


def _bump_graph_row(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    service: str,
    root_cause_class: str,
    last_report_id: str,
    last_seen_at: datetime,
    confidence: float,
    trust_tier: TrustTier,
) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT occurrences, avg_confidence
        FROM service_root_cause_classes
        WHERE tenant_id = ? AND service = ? AND root_cause_class = ?
        """,
        (tenant_id, service, root_cause_class),
    )
    existing = cursor.fetchone()
    if existing is None:
        cursor.execute(
            """
            INSERT INTO service_root_cause_classes (
                tenant_id, service, root_cause_class, occurrences, last_seen_at,
                last_report_id, avg_confidence, trust_tier
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                service,
                root_cause_class,
                last_seen_at.isoformat(),
                last_report_id,
                confidence,
                trust_tier,
            ),
        )
        return

    new_occ = (existing["occurrences"] or 0) + 1
    prev_avg = existing["avg_confidence"] or 0.0
    new_avg = (prev_avg * (new_occ - 1) + confidence) / new_occ
    cursor.execute(
        """
        UPDATE service_root_cause_classes
        SET occurrences = ?, last_seen_at = ?, last_report_id = ?,
            avg_confidence = ?, trust_tier = ?
        WHERE tenant_id = ? AND service = ? AND root_cause_class = ?
        """,
        (
            new_occ,
            last_seen_at.isoformat(),
            last_report_id,
            new_avg,
            trust_tier,
            tenant_id,
            service,
            root_cause_class,
        ),
    )


def _row_to_model(row: sqlite3.Row) -> IncidentRow:
    return IncidentRow(
        report_id=row["report_id"],
        tenant_id=row["tenant_id"],
        alert_id=row["alert_id"],
        service=row["service"],
        root_cause_service=row["root_cause_service"],
        root_cause_class=row["root_cause_class"],
        top_confidence=row["top_confidence"],
        abstained=bool(row["abstained"]),
        trust_tier=row["trust_tier"],
        created_at=datetime.fromisoformat(row["created_at"]),
        report_json=row["report_json"],
    )


# --- root_cause_class taxonomy ----------------------------------------------

# Small heuristic taxonomy. Maps a service name + recommended action into one
# of a fixed set of classes the typed-memory-graph can aggregate over. The set
# is deliberately small to keep the graph dense; it's a starting taxonomy, not
# a final ontology. Override by setting `root_cause_class` on the report or
# patching this map.
_CLASS_RULES: tuple[tuple[str, str], ...] = (
    ("rollback", "deploy_regression"),
    ("revert", "deploy_regression"),
    ("scale", "saturation"),
    ("autoscale", "saturation"),
    ("restart", "process_health"),
    ("flag", "feature_flag"),
    ("config", "config_drift"),
    ("noop", "transient"),
)


def _classify_root_cause(root_cause_service: str, recommended_action: str | None) -> str | None:
    if not recommended_action:
        return None
    text = recommended_action.lower()
    for needle, label in _CLASS_RULES:
        if needle in text:
            return label
    # Fall back to the service-shaped class so we still aggregate.
    if root_cause_service:
        return f"unknown:{root_cause_service.lower()}"
    return None
