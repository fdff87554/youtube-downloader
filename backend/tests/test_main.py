"""Tests for the FastAPI application factory."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class TestCorsConfiguration:
    def test_create_app_raises_when_origins_missing_and_debug_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
        monkeypatch.setenv("DEBUG", "false")

        with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
            create_app()

    def test_create_app_allows_missing_origins_when_debug_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
        monkeypatch.setenv("DEBUG", "true")

        app = create_app()

        assert app.title == "YouTube Downloader API"

    def test_create_app_accepts_explicit_origins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com,https://x.com")
        monkeypatch.setenv("DEBUG", "false")

        app = create_app()

        assert app.title == "YouTube Downloader API"


class TestCorsHeaders:
    """The allow-list only matters if it reaches the response."""

    def _client(self, monkeypatch: pytest.MonkeyPatch, origins: str) -> TestClient:
        monkeypatch.setenv("ALLOWED_ORIGINS", origins)
        monkeypatch.setenv("DEBUG", "false")
        return TestClient(create_app())

    def test_allowed_origin_is_echoed_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(monkeypatch, "https://example.com")

        response = client.get("/api/health", headers={"Origin": "https://example.com"})

        assert response.headers["access-control-allow-origin"] == (
            "https://example.com"
        )

    def test_other_origins_get_no_allow_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(monkeypatch, "https://example.com")

        response = client.get("/api/health", headers={"Origin": "https://evil.com"})

        assert "access-control-allow-origin" not in response.headers

    def test_only_get_is_advertised_on_preflight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(monkeypatch, "https://example.com")

        response = client.options(
            "/api/health",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.headers["access-control-allow-methods"] == "GET"


class TestValidationErrors:
    def test_missing_query_parameter_returns_422(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FastAPI answers its own validation failures with
        # {"detail": [...]}, not the project envelope. Pinned here
        # because the frontend has to tolerate both shapes.
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DEBUG", "false")
        client = TestClient(create_app())

        response = client.get("/api/info")

        assert response.status_code == 422
        assert "detail" in response.json()
        assert "error" not in response.json()


class TestVersion:
    def test_app_version_matches_the_installed_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # app/__init__.py is the single source; pyproject reads it back
        # through setuptools' dynamic version.
        from importlib.metadata import version

        monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DEBUG", "false")

        app = create_app()

        assert app.version == version("youtube-downloader-backend")


class TestInteractiveDocs:
    """Swagger UI is unauthenticated and loads a third-party CDN."""

    def test_docs_are_not_served_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DEBUG", "false")
        client = TestClient(create_app())

        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/openapi.json").status_code == 404

    def test_docs_are_served_in_debug_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DEBUG", "true")
        client = TestClient(create_app())

        assert client.get("/api/docs").status_code == 200
        assert client.get("/api/openapi.json").status_code == 200
