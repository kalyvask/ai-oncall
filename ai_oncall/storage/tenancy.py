"""X-Tenant-Id middleware + row filter.

Every API request carries ``X-Tenant-Id``. When ``settings.tenant_tokens`` is
non-empty, the request must also carry ``Authorization: Bearer <token>`` and
the token must match the configured token for that tenant. This is a v1
auth boundary: pre-shared per-tenant bearer tokens, with OIDC/SSO as
follow-on work.

Every DB row carries ``tenant_id``; the store enforces filtering at query
time so a tenant can never read another tenant's incidents, jobs, or
memory-graph rows.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Request, Response

from ai_oncall.settings import settings

HEADER = "X-Tenant-Id"
AUTH_HEADER = "Authorization"
QUERY_PARAM = "tenant"
# /webhooks/slack/* is public because Slack signs each request with its own
# secret and we recover the tenant from the persisted incident referenced in
# the action payload. No tenant header is possible from Slack's side.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/ready",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
})
PUBLIC_PATH_PREFIXES: tuple[str, ...] = ("/webhooks/slack/",)


class MissingTenantError(Exception):
    """Raised when a request reaches a tenant-scoped endpoint without identifying its tenant."""


class AuthError(Exception):
    """Raised when a request fails per-tenant bearer-token verification."""


def extract_tenant(request: Request) -> str:
    tenant = request.headers.get(HEADER) or request.query_params.get(QUERY_PARAM)
    if not tenant:
        raise MissingTenantError(f"every request must carry {HEADER} header or ?{QUERY_PARAM}=…")
    return tenant


def verify_bearer_token(request: Request, tenant_id: str) -> None:
    """Enforce per-tenant bearer tokens when configured. No-op in dev mode
    (when ``tenant_tokens`` is empty) so local development stays one-line."""
    tokens = settings.tenant_tokens or {}
    if not tokens:
        return
    expected = tokens.get(tenant_id)
    if expected is None:
        raise AuthError(f"tenant {tenant_id!r} is not configured")
    auth_header = request.headers.get(AUTH_HEADER, "")
    if not auth_header.lower().startswith("bearer "):
        raise AuthError("Authorization: Bearer <token> required")
    token = auth_header.split(" ", 1)[1].strip()
    import hmac

    if not hmac.compare_digest(token, expected):
        raise AuthError("bearer token did not match tenant")


async def tenant_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return await call_next(request)
    try:
        tenant_id = extract_tenant(request)
        verify_bearer_token(request, tenant_id)
    except MissingTenantError as exc:
        return Response(content=str(exc), status_code=400)
    except AuthError as exc:
        return Response(content=str(exc), status_code=401)
    request.state.tenant_id = tenant_id
    return await call_next(request)
