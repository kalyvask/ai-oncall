"""FastAPI server.

Endpoints today:
  GET  /health           → liveness, no tenant required.
  POST /webhooks/alert   → Stage 1 RECEIVE. Validates payload, returns the
                            normalized Alert. Tenant comes from X-Tenant-Id;
                            mismatched payload tenant_id is a 400.
  GET  /topology         → Stage 2 ASSEMBLE. Static-yaml snapshot scoped to
                            the request's tenant.

The full agent loop (PLAN+INVESTIGATE+SYNTHESIZE+POST) is added in BRIEF.md
step 6 onwards.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError as ModelValidationError

from ai_oncall.ingest.alerts import receive
from ai_oncall.logging_setup import configure
from ai_oncall.storage.tenancy import tenant_middleware
from ai_oncall.topology.builder import load_static

configure()
app = FastAPI(title="ai-oncall", version="0.0.1")
app.middleware("http")(tenant_middleware)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/webhooks/alert")
async def alert_webhook(request: Request) -> JSONResponse:
    tenant_id: str = request.state.tenant_id
    payload: dict[str, Any] = await request.json()
    if "tenant_id" in payload and payload["tenant_id"] != tenant_id:
        raise HTTPException(400, "payload tenant_id does not match X-Tenant-Id header")
    payload.setdefault("tenant_id", tenant_id)
    try:
        alert = receive(payload)
    except (SchemaValidationError, ModelValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(alert.model_dump(mode="json"))


@app.get("/topology")
def topology(request: Request) -> JSONResponse:
    snapshot = load_static(request.state.tenant_id)
    return JSONResponse(snapshot.model_dump(mode="json", by_alias=True))
