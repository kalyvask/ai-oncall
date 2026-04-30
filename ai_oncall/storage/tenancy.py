"""X-Tenant-Id middleware + row filter. Required reading: BRIEF.md §8 ("Multi-tenancy
without auth"). Every API request carries this header; every DB row carries
tenant_id; the store enforces filtering at query time. No login screen, no RBAC.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Request, Response

HEADER = "X-Tenant-Id"
QUERY_PARAM = "tenant"
PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


class MissingTenantError(Exception):
    """Raised when a request reaches a tenant-scoped endpoint without identifying its tenant."""


def extract_tenant(request: Request) -> str:
    tenant = request.headers.get(HEADER) or request.query_params.get(QUERY_PARAM)
    if not tenant:
        raise MissingTenantError(f"every request must carry {HEADER} header or ?{QUERY_PARAM}=…")
    return tenant


async def tenant_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    try:
        request.state.tenant_id = extract_tenant(request)
    except MissingTenantError as exc:
        return Response(content=str(exc), status_code=400)
    return await call_next(request)
