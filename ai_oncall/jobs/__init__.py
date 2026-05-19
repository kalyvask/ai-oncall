"""Durable job queue for alert→RCA→Slack delivery.

Two job kinds today:
- ``rca``: payload is a serialized Alert; the worker runs ``run_rca`` and
  enqueues a follow-up ``slack_post`` job when a default channel is configured.
- ``slack_post``: payload is ``{report_id, channel}``; the worker loads the
  persisted incident and posts it. Failures retry with exponential backoff.

The store lives in its own SQLite file under ``data/jobs.sqlite`` so the
incidents DB stays read-mostly. Idempotency is enforced on
``(kind, tenant_id, idempotency_key)``; duplicate enqueues return the
existing job rather than creating a new one.
"""

from ai_oncall.jobs.store import (
    JobRecord,
    JobStatus,
    claim_next,
    complete,
    enqueue,
    fail,
    get_job,
    list_jobs,
)
from ai_oncall.jobs.worker import process_one, run_worker

__all__ = [
    "JobRecord",
    "JobStatus",
    "claim_next",
    "complete",
    "enqueue",
    "fail",
    "get_job",
    "list_jobs",
    "process_one",
    "run_worker",
]
