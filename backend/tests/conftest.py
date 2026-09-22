"""Shared test fixtures."""

import pytest
from fastapi.testclient import TestClient

from app.limiter import limiter
from app.main import create_app


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> None:
    """Clear the shared in-memory rate limiter before every test.

    ``limiter`` is a module-level singleton, so per-IP counts survive
    across tests and across TestClient instances. Without this, a test
    that drives a limit to exhaustion makes every later test in the
    session see 429 instead of the status code it asserts -- which made
    the suite depend on declaration order.
    """
    limiter.reset()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a test client for the FastAPI application.

    Sets DEBUG=true so the CORS configuration accepts a missing
    ALLOWED_ORIGINS without raising during create_app(). The rate
    limiter is reset by the autouse ``reset_rate_limiter`` fixture.
    """
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    app = create_app()
    return TestClient(app)
