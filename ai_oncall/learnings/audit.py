"""Tamper-evident audit trail for agent actions.

Every state-changing action (approved rollback, auto-tier dispatch, future
restart / scale / flag flips) is appended to ``data/audit.jsonl`` as one
``AuditRecord`` per line. Each record captures the five governance fields
distilled from EU AI Act + SOC 2 evidence requirements:

  1. ``intent_proposal``     — what the agent (or human approver) intended
  2. ``contextual_state``    — the system view at decision time
  3. ``policy_decision``     — which gate authorised the action and how
  4. ``execution_boundaries``— what bounded the action (tier, scope, expiry)
  5. ``actual_outcome``      — what actually happened, success or failure

Records are hash-chained: each row's ``record_hash`` is
``sha256(prev_hash || canonical_json(payload))``. ``prev_hash`` for the
first row is 64 zeros. ``verify_chain`` re-walks the file and reports the
first index where the chain breaks — sufficient to detect any insertion,
deletion, or in-place edit without a separate signing infrastructure.

This is intentionally not cryptographic signing (no ML-DSA-65 or Ed25519).
The hash chain only proves "the file has not been tampered with since the
last legitimate append" — it does not prove who appended. Pair with the
existing HMAC-signed CD dispatch (``delivery/cd_dispatch.py``) for sender
authentication on the wire.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("AI_ONCALL_DATA_DIR", "data"))
DEFAULT_AUDIT_PATH = DATA_DIR / "audit.jsonl"
GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditRecord:
    """One immutable row in the audit chain.

    ``record_hash`` is derived from every other field plus ``prev_hash`` —
    callers should leave it blank and use :func:`append_audit`, which fills
    both ``prev_hash`` and ``record_hash`` from the file's tail.
    """

    timestamp: str
    tenant_id: str
    report_id: str
    action_id: str
    intent_proposal: str
    contextual_state: dict[str, Any]
    policy_decision: dict[str, Any]
    execution_boundaries: dict[str, Any]
    actual_outcome: dict[str, Any]
    prev_hash: str = GENESIS_HASH
    record_hash: str = ""

    def as_payload(self) -> dict[str, Any]:
        """Fields that go into the hash (everything except ``record_hash``)."""
        payload = asdict(self)
        payload.pop("record_hash", None)
        return payload


@dataclass
class ChainVerification:
    """Result of walking the audit file end-to-end."""

    ok: bool
    rows_checked: int
    broken_at_index: int | None = None
    reason: str | None = None
    broken_rows: list[int] = field(default_factory=list)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(prev_hash: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(_canonical(payload))
    return digest.hexdigest()


def _tail_hash(path: Path) -> str:
    """Return the most recent ``record_hash`` in the file, or genesis if empty."""
    if not path.exists() or path.stat().st_size == 0:
        return GENESIS_HASH
    last_hash = GENESIS_HASH
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            last_hash = row.get("record_hash", last_hash)
    return last_hash


def append_audit(
    *,
    tenant_id: str,
    report_id: str,
    action_id: str,
    intent_proposal: str,
    contextual_state: dict[str, Any],
    policy_decision: dict[str, Any],
    execution_boundaries: dict[str, Any],
    actual_outcome: dict[str, Any],
    path: Path | None = None,
    now: datetime | None = None,
) -> AuditRecord:
    """Append a hash-chained audit record. Returns the persisted record.

    Best-effort on parent-dir creation; raises ``OSError`` if the file
    cannot be opened (callers handle the audit failure separately from the
    primary action).
    """
    target = path or DEFAULT_AUDIT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    prev_hash = _tail_hash(target)
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    record = AuditRecord(
        timestamp=timestamp,
        tenant_id=tenant_id,
        report_id=report_id,
        action_id=action_id,
        intent_proposal=intent_proposal,
        contextual_state=contextual_state,
        policy_decision=policy_decision,
        execution_boundaries=execution_boundaries,
        actual_outcome=actual_outcome,
        prev_hash=prev_hash,
    )
    record_hash = _hash(prev_hash, record.as_payload())
    record = AuditRecord(
        **{**asdict(record), "record_hash": record_hash},
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    return record


def iter_audit(path: Path | None = None) -> Iterator[AuditRecord]:
    """Yield every record in chain order. Skips malformed lines with a warning."""
    target = path or DEFAULT_AUDIT_PATH
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("audit_malformed_line index=%d", i)
                continue
            yield AuditRecord(**row)


def verify_chain(path: Path | None = None) -> ChainVerification:
    """Re-walk the audit file and confirm every ``record_hash`` is correct.

    Reports every row whose ``record_hash`` does not match the recomputed
    hash, or whose ``prev_hash`` does not equal the previous row's hash.
    The first such row is also surfaced as ``broken_at_index`` so callers
    can short-circuit on it.
    """
    target = path or DEFAULT_AUDIT_PATH
    if not target.exists():
        return ChainVerification(ok=True, rows_checked=0)

    expected_prev = GENESIS_HASH
    rows = 0
    broken: list[int] = []
    first_break: int | None = None
    first_reason: str | None = None

    for i, record in enumerate(iter_audit(target)):
        rows += 1
        if record.prev_hash != expected_prev:
            broken.append(i)
            if first_break is None:
                first_break = i
                first_reason = (
                    f"prev_hash mismatch: expected {expected_prev[:12]}…, "
                    f"got {record.prev_hash[:12]}…"
                )
        recomputed = _hash(record.prev_hash, record.as_payload())
        if recomputed != record.record_hash:
            broken.append(i)
            if first_break is None:
                first_break = i
                first_reason = (
                    f"record_hash mismatch: expected {recomputed[:12]}…, "
                    f"got {record.record_hash[:12]}…"
                )
        expected_prev = record.record_hash

    return ChainVerification(
        ok=not broken,
        rows_checked=rows,
        broken_at_index=first_break,
        reason=first_reason,
        broken_rows=sorted(set(broken)),
    )


__all__ = [
    "AuditRecord",
    "ChainVerification",
    "DEFAULT_AUDIT_PATH",
    "GENESIS_HASH",
    "append_audit",
    "iter_audit",
    "verify_chain",
]
