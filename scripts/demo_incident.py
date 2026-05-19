"""Fire a synthetic 'checkout regression' alert at a running ai-oncall API.

Idempotent: re-runs return the same job_id. Polls until the RCA job
completes, then prints the report_id + a link to the incident detail.

Usage:
    python scripts/demo_incident.py \
        --base http://localhost:8000 \
        --tenant demo \
        [--token tok_demo_abc] \
        [--signing-secret shh]

The signing secret is only needed when AI_ONCALL_WEBHOOK_SIGNING_SECRET is
set on the server side. The bearer token is only needed when
AI_ONCALL_TENANT_TOKENS is configured.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import urllib.error
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--token", default="", help="Bearer token (when tenant_tokens is configured)")
    parser.add_argument("--signing-secret", default="", help="HMAC for /webhooks/alert")
    parser.add_argument(
        "--alert",
        default=str(Path(__file__).resolve().parents[1] / "fixtures/synthetic_alerts/checkout_regression.json"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args(argv)

    alert_payload = json.loads(Path(args.alert).read_text(encoding="utf-8"))
    alert_payload.setdefault("tenant_id", args.tenant)
    body = json.dumps(alert_payload).encode("utf-8")

    headers = {
        "X-Tenant-Id": args.tenant,
        "Content-Type": "application/json",
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if args.signing_secret:
        sig = hmac.new(args.signing_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Signature"] = f"hmac-sha256={sig}"

    print(f"POST {args.base}/webhooks/alert  (tenant={args.tenant}, alert_id={alert_payload['alert_id']})")
    req = urllib.request.Request(
        f"{args.base}/webhooks/alert", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            received = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  failed: {e.code} {e.reason} — {e.read().decode('utf-8', 'replace')}")
        return 1
    print(f"  -> 202 job_id={received['job_id']} status={received['status']}")

    job_id = received["job_id"]
    deadline = time.time() + args.timeout_seconds
    last_status = ""
    while time.time() < deadline:
        time.sleep(1.0)
        req = urllib.request.Request(f"{args.base}/jobs/{job_id}", headers=headers)
        with urllib.request.urlopen(req) as resp:
            job = json.loads(resp.read())
        if job["status"] != last_status:
            print(f"  job {job_id}: status={job['status']} attempts={job['attempts']}")
            last_status = job["status"]
        if job["status"] in ("done", "failed"):
            break
    else:
        print(f"  timed out after {args.timeout_seconds}s")
        return 2

    if job["status"] == "failed":
        print(f"  error: {job.get('error')}")
        return 3

    result = job.get("result") or {}
    report_id = result.get("report_id")
    print()
    print(f"RCA ready: {args.base}/incidents/{report_id}")
    print(f"  top_confidence: {result.get('top_confidence')}")
    print(f"  latency_ms:     {result.get('total_latency_ms')}")
    print(f"  cost_usd:       {result.get('cost_usd')}")
    print(f"  slo_violated:   {result.get('slo_violated')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
