"""Tests for playlist-related API behavior."""

from unittest.mock import MagicMock, patch

from app.schemas.video import PlaylistEntry, PlaylistInfo


class TestPlaylistInfoEndpoint:
    @patch("app.routers.info.extract_playlist_info")
    def test_returns_playlist_info_for_playlist_url(
        self, mock_extract: MagicMock, client
    ) -> None:
        mock_extract.return_value = PlaylistInfo(
            playlist_id="PLtest123",
            title="Test Playlist",
            uploader="Creator",
            video_count=2,
            entries=[
                PlaylistEntry(
                    video_id="vid1",
                    title="First Video",
                    duration=120,
                    thumbnail="https://example.com/1.jpg",
                ),
                PlaylistEntry(
                    video_id="vid2",
                    title="Second Video",
                    duration=240,
                    thumbnail="https://example.com/2.jpg",
                ),
            ],
        )

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/playlist?list=PLtest123"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["playlist_id"] == "PLtest123"
        assert data["video_count"] == 2
        assert len(data["entries"]) == 2
        assert data["entries"][0]["video_id"] == "vid1"

    @patch("app.routers.info.extract_playlist_info")
    def test_returns_404_for_unavailable_playlist(
        self, mock_extract: MagicMock, client
    ) -> None:
        from app.services.youtube import VideoNotFoundError

        mock_extract.side_effect = VideoNotFoundError("Playlist not found")

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/playlist?list=PLgone"},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_watch_url_with_list_param_treated_as_video(self, client) -> None:
        """URLs like watch?v=abc&list=PLxxx should be treated as single videos."""
        with patch("app.routers.info.extract_video_info") as mock_video:
            from app.schemas.video import VideoInfo

            mock_video.return_value = VideoInfo(
                video_id="abc",
                title="Test",
                thumbnail="",
                duration=60,
                uploader="Test",
                formats=[],
            )

            response = client.get(
                "/api/info",
                params={"url": "https://www.youtube.com/watch?v=abc&list=PL123"},
            )

            assert response.status_code == 200
            mock_video.assert_called_once()

    @patch("app.routers.info.extract_playlist_info")
    def test_oversized_playlist_returns_400_with_the_limit(
        self, mock_extract: MagicMock, client
    ) -> None:
        # An oversized playlist is bad input, not a server fault: it
        # used to come back as a generic 500 "Could not process this
        # URL", which gave the user nothing to act on.
        from app.services.youtube import MAX_PLAYLIST_SIZE, PlaylistTooLargeError

        mock_extract.side_effect = PlaylistTooLargeError(
            f"This playlist has more than {MAX_PLAYLIST_SIZE} videos."
        )

        response = client.get(
            "/api/info",
            params={"url": "https://www.youtube.com/playlist?list=PLhuge"},
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "playlist_too_large"
        assert str(MAX_PLAYLIST_SIZE) in body["error"]["message"]

    @patch("app.routers.info.extract_playlist_info")
    def test_mixed_case_playlist_url_is_routed_to_the_playlist_extractor(
        self, mock_extract: MagicMock, client
    ) -> None:
        # Normalisation lowercases the host but leaves the path, so the
        # /playlist check has to be case-insensitive on its own.
        mock_extract.return_value = PlaylistInfo(
            playlist_id="PL1",
            title="T",
            uploader="U",
            video_count=0,
            entries=[],
        )

        response = client.get(
            "/api/info",
            params={"url": "https://WWW.YouTube.com/PlayList?list=PL1"},
        )

        assert response.status_code == 200
        mock_extract.assert_called_once()
