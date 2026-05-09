"""Outbound dispatcher: tells the customer's CD system to roll back.

Pluggable on purpose. The default is a signed HTTP POST to a configurable
URL — the customer points it at whatever they use (GitHub Actions
workflow_dispatch, a Spinnaker pipeline, an internal RPC). Each request
carries:

- A clear-text JSON body with the action, service, report_id, and acting
  user.
- An ``X-AI-Oncall-Signature`` header: an HMAC-SHA256 over the body using
  ``settings.cd_dispatch_secret`` so the receiver can verify the sender.

Dry-run mode (no URL configured) returns a ``(False, "dry-run: ...")``
outcome so the calling action handler logs why nothing happened.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover — requests is a top-level dep
    requests = None  # type: ignore[assignment]

from ai_oncall.models import StagedAction
from ai_oncall.settings import settings

logger = logging.getLogger(__name__)


def dispatch_rollback(
    *,
    action: StagedAction,
    report_id: str,
    user_id: str,
    user_name: str,
    target_url: Optional[str] = None,
    timeout_seconds: float = 5.0,
) -> tuple[bool, str]:
    """Send the rollback request to the configured CD endpoint.

    Returns (success, detail_message).
    """
    body = {
        "action": "rollback",
        "kind": action.kind,
        "service": action.service,
        "params": action.params,
        "rationale": action.rationale,
        "runbook_ref": action.runbook_ref,
        "report_id": report_id,
        "approved_by": {"user_id": user_id, "user_name": user_name},
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    body_bytes = json.dumps(body, sort_keys=True).encode("utf-8")

    if not target_url:
        logger.info(
            "cd_dispatch_skipped url=None action=%s service=%s report_id=%s",
            action.kind,
            action.service,
            report_id,
        )
        return False, "dry-run: AI_ONCALL_CD_DISPATCH_URL not configured"

    if requests is None:
        return False, "dry-run: requests library unavailable"

    secret = getattr(settings, "cd_dispatch_secret", "") or ""
    headers = {"Content-Type": "application/json"}
    if secret:
        sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        headers["X-AI-Oncall-Signature"] = f"sha256={sig}"

    try:
        resp = requests.post(
            target_url, data=body_bytes, headers=headers, timeout=timeout_seconds
        )
    except Exception as e:
        logger.exception("cd_dispatch_request_failed")
        return False, f"dispatch request failed: {e}"

    if resp.status_code >= 200 and resp.status_code < 300:
        return True, f"dispatched (status {resp.status_code})"
    return False, f"dispatch returned status {resp.status_code}: {resp.text[:200]}"


def sign_dispatch_body(body: bytes, secret: str) -> str:
    """Helper exposed for tests and for the receiver to mirror."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
