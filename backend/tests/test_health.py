"""Tests for the health check endpoint.

It is what the container's HEALTHCHECK polls, so a change in its path
or payload silently turns the readiness signal off.
"""

from fastapi.testclient import TestClient


class TestHealthCheck:
    def test_returns_ok(self, client: TestClient) -> None:
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_is_not_rate_limited(self, client: TestClient) -> None:
        # The container polls this every 30s and it carries no
        # @limiter.limit decorator; only nginx throttles it.
        for _ in range(40):
            response = client.get("/api/health")

        assert response.status_code == 200
