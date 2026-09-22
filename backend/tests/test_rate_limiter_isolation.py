"""Guards against rate-limit state leaking between tests.

``app.limiter.limiter`` is a module-level singleton shared by every
TestClient, so without an explicit reset the suite's result depends on
declaration order. These two tests fail as a pair if the autouse
``reset_rate_limiter`` fixture in conftest.py is removed.
"""

from fastapi.testclient import TestClient

DOWNLOAD_LIMIT_PER_MINUTE = 5
INVALID_URL = "not-a-youtube-url"


class TestRateLimiterIsolation:
    def test_download_returns_429_once_the_per_minute_limit_is_passed(
        self, client: TestClient
    ) -> None:
        for _ in range(DOWNLOAD_LIMIT_PER_MINUTE + 1):
            response = client.get("/api/download", params={"url": INVALID_URL})

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"

    def test_download_budget_is_full_again_in_the_next_test(
        self, client: TestClient
    ) -> None:
        for _ in range(DOWNLOAD_LIMIT_PER_MINUTE):
            response = client.get("/api/download", params={"url": INVALID_URL})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_url"
