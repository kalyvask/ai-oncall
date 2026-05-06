"""Code-and-change correlation. Roadmap item 4.

Runs after SYNTHESIZE on the RcaReport. For each ranked hypothesis with a
`root_cause_service`, fetches the most recent deploy on that service before
the alert fired and attaches its diff as evidence. If the local store has a
populated `patch_excerpt` we use it; otherwise we ask the GitHub client (if
configured) for it. Hypotheses are not reordered or rewritten — only their
evidence list grows.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ai_oncall.models import EvidenceItem, RcaReport
from ai_oncall.storage.base import TelemetryStore
from ai_oncall.storage.github import GitHubClient

logger = logging.getLogger(__name__)

DEPLOY_LOOKBACK = timedelta(hours=24)
EVIDENCE_PATCH_MAX = 800  # one evidence row, not the whole patch
EVIDENCE_MAX_PER_HYPOTHESIS = 8  # mirrors EvidenceItem schema bound


def correlate_changes(
    report: RcaReport,
    store: TelemetryStore,
    github: GitHubClient | None = None,
) -> RcaReport:
    since = report.alert.fired_at - DEPLOY_LOOKBACK
    seen_changes: dict[str, dict] = {}

    for hypothesis in report.hypotheses:
        if len(hypothesis.evidence) >= EVIDENCE_MAX_PER_HYPOTHESIS:
            continue
        service = hypothesis.root_cause_service
        try:
            deploys = store.recent_deploys(report.tenant_id, service, since)
        except NotImplementedError:
            continue
        if not deploys:
            continue
        most_recent = max(deploys, key=lambda d: d.timestamp)

        excerpt = most_recent.patch_excerpt or ""
        if not excerpt and github is not None and most_recent.sha:
            commit = github.fetch_commit_patch(most_recent.sha)
            if commit is not None:
                excerpt = commit.patch_excerpt
                seen_changes[most_recent.event_id] = {
                    "title": commit.title,
                    "actor": commit.actor,
                    "url": commit.url,
                    "files_changed": commit.files_changed,
                }
        if not excerpt:
            continue

        claim = (
            f"Last deploy on {service} before the alert: "
            f"{most_recent.title or most_recent.sha or most_recent.event_id} "
            f"by {most_recent.actor} at {most_recent.timestamp.isoformat()}"
        )
        evidence = EvidenceItem(claim=claim, source=excerpt[:EVIDENCE_PATCH_MAX])
        hypothesis.evidence.append(evidence)
    return report
