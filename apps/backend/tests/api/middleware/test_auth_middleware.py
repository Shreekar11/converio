"""Hermetic tests for the JWT authentication middleware against the new
self-serve `/api/v1/auth/*` endpoints.

These tests assert the middleware's *gate* behaviour: an unauthenticated
request to each new auth endpoint must short-circuit with 401 before the
endpoint's handler (and its dependencies) ever execute. They explicitly
defend against regressions where these paths might be added to
`EXCLUDED_PATHS`, or where a prefix-style exclusion accidentally lets
`/api/v1/auth/*` slip through.

Strategy (per `tests/api/test_companies_endpoints.py`):
- Build a minimal FastAPI app that mounts the auth router under the
  production prefix (`/api/v1/auth`) and installs only the
  `JWTAuthenticationMiddleware`. No DB, no JWT verifier — the middleware
  rejects on missing token before any of those are touched.
- Assert each path returns 401 (not 200, not 404, not 501).
"""
from __future__ import annotations

from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from app.api.v1.endpoints import auth as auth_module
from app.api.v1.middleware.auth import JWTAuthenticationMiddleware


def _build_app() -> FastAPI:
    """Mount the auth router under `/api/v1/auth` with the JWT middleware
    in front. Mirrors the production wiring (`main.py` mounts the v1
    router at `settings.api_v1_prefix`, default `/api/v1`).
    """
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/api/v1/auth")
    app.add_middleware(JWTAuthenticationMiddleware)
    return app


def _client() -> TestClient:
    return TestClient(_build_app())


# ---------------------------------------------------------------------------
# /api/v1/auth/* — middleware enforces auth on every new endpoint
# ---------------------------------------------------------------------------


def test_get_auth_me_requires_auth() -> None:
    with _client() as client:
        resp = client.get("/api/v1/auth/me")

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp.json()["detail"] == "Authentication required"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_company_signup_requires_auth() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/auth/company/signup",
            json={"company_name": "Acme", "full_name": "Jane Doe"},
        )

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp.json()["detail"] == "Authentication required"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_recruiter_signup_requires_auth() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/auth/recruiter/signup",
            json={"full_name": "Jane Doe"},
        )

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp.json()["detail"] == "Authentication required"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
