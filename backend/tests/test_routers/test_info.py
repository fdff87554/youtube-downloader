"""Tests for the video info and format listing API endpoints."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.video import VideoInfo


class TestGetInfo:
    @patch("app.routers.info.extract_video_info")
    def test_get_info_with_video_url_returns_video_metadata(
        self, mock_extract: MagicMock, client
    ) -> None:
        mock_extract.return_value = VideoInfo(
            video_id="test123",
            title="Test Video",
            thumbnail="https://example.com/thumb.jpg",
            duration=120,
            uploader="Test Channel",
            formats=[],
        )

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/watch?v=test123"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["video_id"] == "test123"
        assert data["title"] == "Test Video"

    @patch("app.routers.info.extract_video_info")
    def test_get_info_with_invalid_url_returns_400(
        self, mock_extract: MagicMock, client
    ) -> None:
        from app.services.youtube import InvalidURLError

        mock_extract.side_effect = InvalidURLError("URL must be a valid YouTube URL.")

        response = client.get(
            "/api/info",
            params={"url": "https://example.com/video"},
        )

        assert response.status_code == 400
        data = response.json()
        assert data["error"]["code"] == "invalid_url"

    @patch("app.routers.info.extract_video_info")
    def test_get_info_with_unavailable_video_returns_404(
        self, mock_extract: MagicMock, client
    ) -> None:
        from app.services.youtube import VideoNotFoundError

        mock_extract.side_effect = VideoNotFoundError("Video is unavailable")

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/watch?v=gone"},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    @patch("app.routers.info.extract_playlist_info")
    def test_get_info_with_playlist_url_returns_playlist_metadata(
        self, mock_extract: MagicMock, client
    ) -> None:
        from app.schemas.video import PlaylistInfo

        mock_extract.return_value = PlaylistInfo(
            playlist_id="PLtest",
            title="Test Playlist",
            uploader="Creator",
            video_count=0,
            entries=[],
        )

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/playlist?list=PLtest"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["playlist_id"] == "PLtest"


class TestEventLoopIsNotBlocked:
    """yt-dlp extraction must not run on the event loop thread."""

    @patch("app.routers.info.extract_video_info")
    def test_extraction_runs_off_the_event_loop(
        self, mock_extract: MagicMock, client: TestClient
    ) -> None:
        # asyncio.get_running_loop() only succeeds on the thread that
        # runs the loop. If get_info were `async def`, the blocking
        # extraction would execute there and freeze every other
        # request; as a sync endpoint FastAPI offloads it to a worker.
        observed: dict[str, bool] = {}

        def record_execution_context(url: str) -> VideoInfo:
            try:
                asyncio.get_running_loop()
                observed["on_event_loop"] = True
            except RuntimeError:
                observed["on_event_loop"] = False
            return VideoInfo(
                video_id="test",
                title="Test",
                thumbnail="",
                duration=10,
                uploader="x",
                formats=[],
            )

        mock_extract.side_effect = record_execution_context

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 200
        assert observed["on_event_loop"] is False


class TestErrorMasking:
    """Verify upstream error detail is hidden when DEBUG is off."""

    def _make_client(self, monkeypatch: pytest.MonkeyPatch, debug: bool) -> TestClient:
        monkeypatch.setenv("DEBUG", "true" if debug else "false")
        monkeypatch.setenv("ALLOWED_ORIGINS", "*")
        return TestClient(create_app())

    @patch("app.routers.info.extract_video_info")
    def test_500_message_hides_detail_when_debug_off(
        self, mock_extract: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.youtube import YouTubeError

        sensitive = "yt-dlp internal trace at /tmp/secret-path"
        mock_extract.side_effect = YouTubeError(sensitive)
        client = self._make_client(monkeypatch, debug=False)

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 500
        body = response.json()
        assert body["error"]["code"] == "extraction_error"
        assert sensitive not in body["error"]["message"]
        assert "secret-path" not in body["error"]["message"]

    @patch("app.routers.info.extract_video_info")
    def test_500_message_includes_detail_when_debug_on(
        self, mock_extract: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.youtube import YouTubeError

        mock_extract.side_effect = YouTubeError("specific yt-dlp error 42")
        client = self._make_client(monkeypatch, debug=True)

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 500
        body = response.json()
        assert "specific yt-dlp error 42" in body["error"]["message"]


class TestRateLimit:
    @patch("app.routers.info.extract_video_info")
    def test_returns_429_after_info_limit_exceeded(
        self, mock_extract: MagicMock, client
    ) -> None:
        mock_extract.return_value = VideoInfo(
            video_id="test",
            title="Test",
            thumbnail="",
            duration=10,
            uploader="x",
            formats=[],
        )

        # Limit is 30/minute. Drive past it from a single client.
        last_response = None
        for _ in range(31):
            last_response = client.get(
                "/api/info",
                params={"url": "https://www.youtube.com/watch?v=test"},
            )
            if last_response.status_code == 429:
                break

        assert last_response is not None
        assert last_response.status_code == 429
        assert last_response.json()["error"]["code"] == "rate_limited"
