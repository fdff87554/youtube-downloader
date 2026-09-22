"""Tests for the download streaming API endpoint."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from app.main import create_app
from app.services.youtube import YouTubeError


class TestDownloadVideo:
    @patch("app.routers.download.stream_download")
    def test_download_with_mp4_returns_streaming_video_response(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        mock_stream.return_value = iter([b"fake video data"])

        response = client.get(
            "/api/download",
            params={
                "url": "https://www.youtube.com/watch?v=test",
                "fmt": "mp4",
                "quality": "best",
                "title": "Test Video",
            },
        )

        assert response.status_code == 200
        assert "attachment" in response.headers.get("content-disposition", "")
        assert response.headers.get("content-type") == "video/mp4"

    @patch("app.routers.download.stream_download")
    def test_download_with_mp3_returns_audio_mpeg_response(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        mock_stream.return_value = iter([b"fake audio data"])

        response = client.get(
            "/api/download",
            params={
                "url": "https://www.youtube.com/watch?v=test",
                "fmt": "mp3",
            },
        )

        assert response.status_code == 200
        assert response.headers.get("content-type") == "audio/mpeg"

    def test_download_with_invalid_url_returns_400(self, client) -> None:
        response = client.get(
            "/api/download",
            params={"url": "https://example.com/video"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_url"

    @patch("app.routers.download.stream_download")
    def test_download_when_stream_fails_returns_500(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        from app.services.youtube import YouTubeError

        # stream_download is a generator function, so a real failure
        # surfaces on the first next() -- not when it is called. Using
        # side_effect here would test a path production cannot reach.
        def failing_generator():
            raise YouTubeError("download failed")
            yield b""  # pragma: no cover - makes this a generator

        mock_stream.return_value = failing_generator()

        response = client.get(
            "/api/download",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "download_error"

    @patch("app.routers.download.stream_download")
    def test_download_of_unavailable_video_returns_404(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        from app.services.youtube import VideoNotFoundError

        def unavailable_generator():
            raise VideoNotFoundError("Video is unavailable: ERROR: Private video")
            yield b""  # pragma: no cover - makes this a generator

        mock_stream.return_value = unavailable_generator()

        response = client.get(
            "/api/download",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    @patch("app.routers.download.stream_download")
    def test_download_of_unsupported_url_returns_400(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        from app.services.youtube import UnsupportedURLError

        def unsupported_generator():
            raise UnsupportedURLError("No suitable extractor found for URL x")
            yield b""  # pragma: no cover - makes this a generator

        mock_stream.return_value = unsupported_generator()

        response = client.get(
            "/api/download",
            params={"url": "https://www.youtube.com/about/xyz"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_url"

    @patch("app.routers.download.stream_download")
    def test_download_of_unavailable_format_returns_400(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        # Asking for 1080 on a video that has no such format is about
        # the request, not a missing video, so it must not be a 404.
        from app.services.youtube import FormatUnavailableError

        def no_such_format():
            raise FormatUnavailableError("Requested format is not available")
            yield b""  # pragma: no cover - makes this a generator

        mock_stream.return_value = no_such_format()

        response = client.get(
            "/api/download",
            params={"url": "https://www.youtube.com/watch?v=test", "quality": "1080"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "format_unavailable"

    @patch("app.routers.download.stream_download")
    def test_download_yielding_no_data_returns_404_not_empty_200(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        # Regression guard: an empty stream used to be served as a
        # complete 200 with a 0-byte body, indistinguishable from a
        # successful download.
        mock_stream.return_value = iter([])

        response = client.get(
            "/api/download",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_download_without_url_returns_422(self, client) -> None:
        response = client.get("/api/download")

        assert response.status_code == 422

    @patch("app.routers.download.stream_download")
    def test_download_with_title_uses_it_in_content_disposition(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        mock_stream.return_value = iter([b"data"])

        response = client.get(
            "/api/download",
            params={
                "url": "https://www.youtube.com/watch?v=test",
                "title": "My Video",
            },
        )

        assert response.status_code == 200
        disposition = response.headers.get("content-disposition", "")
        assert "My%20Video" in disposition

    @patch("app.routers.download.stream_download")
    def test_download_without_title_uses_default_filename(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        mock_stream.return_value = iter([b"data"])

        response = client.get(
            "/api/download",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 200
        disposition = response.headers.get("content-disposition", "")
        assert "download.mp4" in disposition

    @patch("app.routers.download.stream_download")
    def test_download_failure_mid_stream_does_not_become_json_envelope(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        """Once StreamingResponse starts, a generator-time failure cannot
        be turned into the standard JSON 5xx envelope -- HTTP headers
        have already been written.

        In a real ASGI deployment the client sees a truncated body /
        ERR_INCOMPLETE_CHUNKED_ENCODING. Under TestClient the exception
        propagates to the caller (see anyio task-group surface). Either
        way the response cannot be a JSON envelope claiming success.

        This test pins that contract so future contributors do not add
        a try/except around StreamingResponse and assume it can yield
        a JSON 5xx body.
        """
        from app.services.youtube import YouTubeError

        def failing_generator():
            yield b"partial"
            raise YouTubeError("simulated yt-dlp crash mid-stream")

        mock_stream.return_value = failing_generator()

        with (
            pytest.raises(YouTubeError, match="simulated yt-dlp"),
            client.stream(
                "GET",
                "/api/download",
                params={"url": "https://www.youtube.com/watch?v=test"},
            ) as response,
        ):
            # Headers were already on the wire before the failure.
            assert response.status_code == 200
            assert response.headers["content-type"] == "video/mp4"
            for _ in response.iter_bytes():
                pass


class TestEventLoopIsNotBlocked:
    """Waiting for yt-dlp's first bytes must not run on the event loop."""

    @patch("app.routers.download.stream_download")
    def test_first_chunk_is_read_off_the_event_loop(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        # asyncio.get_running_loop() only succeeds on the thread running
        # the loop. The first next() on the stream can wait seconds for
        # yt-dlp to start producing, so doing it there would stall every
        # other request -- including in-flight downloads.
        observed: dict[str, bool] = {}

        def recording_generator():
            try:
                asyncio.get_running_loop()
                observed["on_event_loop"] = True
            except RuntimeError:
                observed["on_event_loop"] = False
            yield b"data"

        mock_stream.return_value = recording_generator()

        response = client.get(
            "/api/download",
            params={"url": "https://www.youtube.com/watch?v=test"},
        )

        assert response.status_code == 200
        assert observed["on_event_loop"] is False


class TestPipelineCleanupIsReachableFromTheResponse:
    """Issue #105: the generator's finally cannot clean up a disconnect.

    Starlette wraps the sync response iterator in iterate_in_threadpool,
    which never calls close() on it, so a cancelled response abandons
    the generator instead of closing it and yt-dlp and ffmpeg keep
    running. The response's background task is the hook that does run,
    so the endpoint has to hand the pipeline a handle it can close from
    there rather than relying on the generator unwinding.
    """

    @patch("app.routers.download.stream_download")
    def test_pipeline_handle_is_passed_to_the_service(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        mock_stream.return_value = iter([b"data"])

        with patch("app.routers.download.DownloadProcesses") as processes_cls:
            handle = processes_cls.return_value
            client.get(
                "/api/download",
                params={"url": "https://www.youtube.com/watch?v=test"},
            )

        assert mock_stream.call_args.kwargs["processes"] is handle

    @patch("app.routers.download.stream_download")
    def test_pipeline_is_closed_once_the_response_is_done(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        mock_stream.return_value = iter([b"data"])

        with patch("app.routers.download.DownloadProcesses") as processes_cls:
            handle = processes_cls.return_value
            response = client.get(
                "/api/download",
                params={"url": "https://www.youtube.com/watch?v=test"},
            )

        assert response.status_code == 200
        handle.close.assert_called_once_with()

    @patch("app.routers.download.stream_download")
    def test_no_background_task_when_the_download_fails_to_start(
        self,
        mock_stream: MagicMock,
        client,
    ) -> None:
        # The failure surfaces before StreamingResponse exists, so the
        # generator's own finally is what cleaned up; there is no
        # response left to carry a background task.
        def failing_generator():
            raise YouTubeError("yt-dlp failed")
            yield b""  # pragma: no cover - generator marker

        mock_stream.return_value = failing_generator()

        with patch("app.routers.download.DownloadProcesses") as processes_cls:
            handle = processes_cls.return_value
            response = client.get(
                "/api/download",
                params={"url": "https://www.youtube.com/watch?v=test"},
            )

        assert response.status_code == 500
        handle.close.assert_not_called()


class TestAsgiDisconnectRunsTheBackgroundTask:
    """Pins the integration point the disconnect fix depends on.

    The fix rests on an observed Starlette behaviour: a cancelled
    StreamingResponse still awaits its background task, while the sync
    body iterator it abandons is never closed
    (starlette/responses.py, StreamingResponse.__call__). TestClient
    cannot abort mid-response, so this drives the ASGI app directly and
    answers http.disconnect after the first body chunk.

    stream_download is mocked, so the generator here never calls
    close() itself. That makes the assertion precise: only the
    background task can have called it.
    """

    SCOPE = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/download",
        "raw_path": b"/api/download",
        "query_string": b"url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dtest",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    def _disconnect_mid_stream(self, app) -> tuple[list[str], list[bool]]:
        """Run one request that disconnects after the first body chunk.

        Returns the ASGI message types sent, and whether the response
        generator's finally block ran.
        """
        finally_ran: list[bool] = []

        def endless_chunks():
            try:
                while True:
                    yield b"x" * 1024
                    time.sleep(0.01)
            finally:
                finally_ran.append(True)

        sent: list[str] = []

        async def drive() -> None:
            disconnected = asyncio.Event()

            async def receive() -> dict[str, str]:
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message) -> None:
                sent.append(message["type"])
                if message["type"] == "http.response.body":
                    disconnected.set()

            with patch(
                "app.routers.download.stream_download",
                return_value=endless_chunks(),
            ):
                await app(self.SCOPE, receive, send)

        asyncio.run(drive())
        return sent, finally_ran

    def test_pipeline_is_closed_when_the_client_disconnects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEBUG", "true")
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
        app = create_app()

        with patch("app.routers.download.DownloadProcesses") as processes_cls:
            handle = processes_cls.return_value
            sent, finally_ran = self._disconnect_mid_stream(app)

        assert "http.response.body" in sent, "the response never started streaming"
        # The point of the fix: the abandoned generator is not the hook.
        assert not finally_ran, "the generator was closed, so this proves nothing"
        handle.close.assert_called_once_with()
