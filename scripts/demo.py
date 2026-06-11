"""One-command demo: boot the API with the mock LLM, fire a synthetic alert,
print the resulting RCA. No API key, no Prometheus, no Slack.

Usage:
    python scripts/demo.py            # run the demo, keep the API serving
    python scripts/demo.py --once     # run the demo, then shut down (smoke test)
    python scripts/demo.py --port 8123

What it does:
1. Starts ``uvicorn ai_oncall.server:app`` as a subprocess with the mock LLM
   provider (the default) so no credentials are needed.
2. Waits for ``/ready``.
3. POSTs ``fixtures/synthetic_alerts/checkout_regression.json`` to
   ``/webhooks/alert`` and polls the job until the RCA completes
   (this reuses scripts/demo_incident.py).
4. Fetches the stored report and prints the top hypothesis, its evidence,
   and the recommended action.
5. Leaves the API running so you can open the web UI against it
   (``cd web && npm run dev``), unless ``--once`` was given.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import demo_incident  # noqa: E402


def _wait_ready(base: str, timeout_seconds: float = 30.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/ready", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _fetch_report(base: str, tenant: str) -> dict | None:
    headers = {"X-Tenant-Id": tenant}
    req = urllib.request.Request(f"{base}/incidents?limit=1", headers=headers)
    with urllib.request.urlopen(req) as resp:
        items = json.loads(resp.read())["items"]
    if not items:
        return None
    report_id = items[0]["report_id"]
    req = urllib.request.Request(f"{base}/incidents/{report_id}", headers=headers)
    with urllib.request.urlopen(req) as resp:
        data: dict = json.loads(resp.read())
        return data


def _print_report(report: dict) -> None:
    top = report["hypotheses"][0]
    inv = report.get("investigation") or {}
    print()
    print("=" * 72)
    print(f"RCA report {report['report_id']}")
    print(f"Alert:       {report['alert']['title']}")
    print(f"Root cause:  {top['root_cause_service']}  (confidence {top['confidence']:.2f})")
    print(f"Reasoning:   {top['reasoning']}")
    print("Evidence:")
    for item in top["evidence"]:
        print(f"  - {item['claim']}  [{item['source']}]")
    print(f"Action:      {top['recommended_action']}")
    calls = inv.get("tool_calls") or []
    if calls:
        print(f"Tool calls:  {len(calls)} ({', '.join(c['tool'] for c in calls)})")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    # Report text contains non-ASCII (service arrows etc.); don't let a
    # cp1252 Windows console kill the demo at the finish line.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--once", action="store_true", help="exit after printing the report")
    args = parser.parse_args(argv)
    base = f"http://127.0.0.1:{args.port}"

    env = dict(os.environ)
    env.setdefault("AI_ONCALL_DEMO", "1")
    env.setdefault("AI_ONCALL_LLM_PROVIDER", "mock")
    env.setdefault("AI_ONCALL_TELEMETRY_STORE", "sqlite")

    print(f"Starting ai-oncall API on {base} (mock LLM, sqlite store)...")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ai_oncall.server:app", "--port", str(args.port)],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_ready(base):
            print("Server did not become ready within 30s.", file=sys.stderr)
            return 1
        rc = demo_incident.main(["--base", base, "--tenant", args.tenant])
        if rc != 0:
            return rc
        report = _fetch_report(base, args.tenant)
        if report is None:
            print("No incident found after the job completed.", file=sys.stderr)
            return 1
        _print_report(report)
        if args.once:
            return 0
        print()
        print(f"API still serving on {base} — Ctrl+C to stop.")
        print("Web UI: cd web && npm install && npm run dev  (NEXT_PUBLIC_API_BASE defaults ok)")
        try:
            server.wait()
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    sys.exit(main())
