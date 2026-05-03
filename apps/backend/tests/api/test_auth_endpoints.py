"""Smoke tests for the self-serve auth router skeleton (T2.2).

The auth endpoints are stubbed (501 Not Implemented) at this point in the
plan. These tests pin two behaviours we never want to regress while the
real handlers are filled in:

- The router is mounted under `/auth` and the `GET /me` route exists.
- A request with no `Authorization` header is rejected by the
  `get_current_user` dependency before the stub body runs (401, not 501).

Strategy mirrors `tests/api/test_companies_endpoints.py`: build a minimal
FastAPI app with only the auth router mounted, so the production JWT
middleware is out of scope. The `HTTPBearer(auto_error=False)` security
scheme on `get_current_user` lets the dependency itself raise 401 when
no credentials are supplied.
"""
from __future__ import annotations

from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from app.api.v1.endpoints import auth as auth_module


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/auth")
    return app


def test_router_registered_requires_auth() -> None:
    """`GET /auth/me` without a Bearer token must return 401, not 501.

    This proves both that the router is wired up (any other path would 404)
    and that `get_current_user` runs before the stub handler — so once the
    handler is implemented, unauthenticated callers will continue to be
    rejected at the dependency layer.
    """
    with TestClient(_build_app()) as client:
        resp = client.get("/auth/me")

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text
    assert resp.json()["detail"] == "Authorization header missing"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
