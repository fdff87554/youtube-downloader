"""Unit tests for the YouTube service layer."""

import signal
from unittest.mock import MagicMock, patch

import pytest

from app.services.youtube import (
    InvalidURLError,
    VideoNotFoundError,
    _base_opts,
    _build_audio_command,
    _build_video_command,
    _finalize_process,
    _resolve_video_format,
    build_download_filename,
    extract_playlist_info,
    extract_video_info,
    validate_youtube_url,
)


def _permitted_extractors_for(url: str) -> list[str]:
    """Extractor names that both match `url` and pass the allow-list.

    Mirrors what YoutubeDL does at construction time: the configured
    patterns are regexes matched against lowercased extractor names.
    """
    from yt_dlp.extractor import gen_extractor_classes
    from yt_dlp.utils import orderedSet_from_options

    from app.services.youtube import ALLOWED_EXTRACTORS

    all_ies = {ie.IE_NAME.lower(): ie for ie in gen_extractor_classes()}
    permitted = set(
        orderedSet_from_options(
            list(ALLOWED_EXTRACTORS),
            {"all": list(all_ies), "default": list(all_ies)},
            use_regex=True,
        )
    )
    return [
        name
        for name, ie in all_ies.items()
        if name in permitted and name != "generic" and ie.suitable(url)
    ]


class TestValidateYoutubeUrl:
    def test_valid_youtube_url_accepted(self) -> None:
        validate_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_valid_youtu_be_url_accepted(self) -> None:
        validate_youtube_url("https://youtu.be/dQw4w9WgXcQ")

    def test_valid_mobile_url_accepted(self) -> None:
        validate_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_uppercase_host_accepted(self) -> None:
        # Host and scheme are case-insensitive, and URLs get pasted in
        # with whatever casing their source used.
        validate_youtube_url("https://WWW.YouTube.com/watch?v=dQw4w9WgXcQ")
        validate_youtube_url("HTTPS://YOUTU.BE/dQw4w9WgXcQ")

    def test_non_youtube_url_raises_error(self) -> None:
        with pytest.raises(InvalidURLError, match="valid YouTube URL"):
            validate_youtube_url("https://vimeo.com/12345")

    def test_lookalike_hosts_still_rejected(self) -> None:
        # The "/" after the host is what rejects these; the
        # case-insensitive flag must not widen the pattern.
        for url in (
            "https://youtube.com@evil.com/watch?v=x",
            "https://youtube.com.evil.com/watch?v=x",
            "https://notyoutube.com/watch?v=x",
            "https://YOUTUBE.COM.EVIL.COM/watch?v=x",
        ):
            with pytest.raises(InvalidURLError):
                validate_youtube_url(url)

    def test_empty_string_raises_error(self) -> None:
        with pytest.raises(InvalidURLError):
            validate_youtube_url("")

    def test_plain_text_raises_error(self) -> None:
        with pytest.raises(InvalidURLError):
            validate_youtube_url("not a url at all")


class TestExtractVideoInfo:
    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_returns_video_info_for_valid_video(self, mock_ydl_cls: MagicMock) -> None:
        # Arrange
        mock_info = {
            "id": "dQw4w9WgXcQ",
            "title": "Test Video",
            "thumbnail": "https://img.youtube.com/vi/dQw4w9WgXcQ/0.jpg",
            "duration": 212,
            "uploader": "Test Channel",
            "formats": [
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "height": 1080,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "filesize": 50000000,
                },
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "height": None,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "filesize": 3000000,
                },
            ],
        }
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        # Act
        result = extract_video_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        # Assert
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.title == "Test Video"
        assert result.duration == 212
        assert result.uploader == "Test Channel"
        assert len(result.formats) == 2

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_returns_none_info_raises_not_found(self, mock_ydl_cls: MagicMock) -> None:
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = None
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(VideoNotFoundError):
            extract_video_info("https://www.youtube.com/watch?v=nonexistent")

    def test_non_youtube_url_raises_invalid(self) -> None:
        with pytest.raises(InvalidURLError):
            extract_video_info("https://example.com/video")

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_private_video_raises_not_found(self, mock_ydl_cls: MagicMock) -> None:
        import yt_dlp

        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "Video unavailable"
        )
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(VideoNotFoundError):
            extract_video_info("https://www.youtube.com/watch?v=private123")


class TestExtractPlaylistInfo:
    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_rejects_playlist_above_size_limit(self, mock_ydl_cls: MagicMock) -> None:
        from app.services.youtube import MAX_PLAYLIST_SIZE, PlaylistTooLargeError

        mock_info = {
            "id": "PLhuge",
            "title": "Huge Playlist",
            "uploader": "Creator",
            "entries": [
                {"id": f"v{i}", "title": f"Video {i}", "duration": 60, "thumbnail": ""}
                for i in range(MAX_PLAYLIST_SIZE + 1)
            ],
        }
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(PlaylistTooLargeError, match=str(MAX_PLAYLIST_SIZE)):
            extract_playlist_info("https://www.youtube.com/playlist?list=PLhuge")

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_asks_ytdlp_to_stop_just_past_the_limit(
        self, mock_ydl_cls: MagicMock
    ) -> None:
        # Truncating at the source is what keeps an oversized playlist
        # from costing a full extraction before being rejected.
        from app.services.youtube import MAX_PLAYLIST_SIZE

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {"id": "PL", "entries": []}
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        extract_playlist_info("https://www.youtube.com/playlist?list=PL")

        opts = mock_ydl_cls.call_args[0][0]
        assert opts["playlistend"] == MAX_PLAYLIST_SIZE + 1

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_returns_playlist_with_entries(self, mock_ydl_cls: MagicMock) -> None:
        mock_info = {
            "id": "PLtest123",
            "title": "Test Playlist",
            "uploader": "Playlist Creator",
            "entries": [
                {
                    "id": "vid1",
                    "title": "First Video",
                    "duration": 100,
                    "thumbnail": "https://img.youtube.com/vi/vid1/0.jpg",
                },
                {
                    "id": "vid2",
                    "title": "Second Video",
                    "duration": 200,
                    "thumbnail": "https://img.youtube.com/vi/vid2/0.jpg",
                },
            ],
        }
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        result = extract_playlist_info(
            "https://www.youtube.com/playlist?list=PLtest123"
        )

        assert result.playlist_id == "PLtest123"
        assert result.title == "Test Playlist"
        assert result.video_count == 2
        assert result.entries[0].video_id == "vid1"
        assert result.entries[1].title == "Second Video"

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_skips_none_entries(self, mock_ydl_cls: MagicMock) -> None:
        mock_info = {
            "id": "PLtest",
            "title": "Test",
            "uploader": "Creator",
            "entries": [
                None,
                {
                    "id": "vid1",
                    "title": "Video",
                    "duration": 100,
                    "thumbnail": "https://example.com/thumb.jpg",
                },
            ],
        }
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        result = extract_playlist_info("https://www.youtube.com/playlist?list=PLtest")

        assert result.video_count == 1


class TestBuildDownloadFilename:
    def test_returns_sanitized_filename(self) -> None:
        result = build_download_filename('Test: "Video" Title', "mp4")

        assert result == "Test_ _Video_ Title.mp4"
        assert '"' not in result
        assert ":" not in result

    def test_returns_mp3_extension_for_audio(self) -> None:
        result = build_download_filename("Audio Track", "mp3")

        assert result.endswith(".mp3")

    def test_fallback_filename_when_title_is_none(self) -> None:
        result = build_download_filename(None, "mp4")

        assert result == "download.mp4"

    def test_fallback_filename_when_title_is_empty(self) -> None:
        result = build_download_filename("", "mp4")

        assert result == "download.mp4"


class TestCacheDisabled:
    def test_base_opts_disable_cachedir(self) -> None:
        assert _base_opts()["cachedir"] is False

    def test_video_command_passes_no_cache_dir(self) -> None:
        cmd = _build_video_command("https://www.youtube.com/watch?v=test", "best")
        assert "--no-cache-dir" in cmd

    def test_audio_command_passes_no_cache_dir(self) -> None:
        cmd = _build_audio_command("https://www.youtube.com/watch?v=test")
        assert "--no-cache-dir" in cmd


class TestConfigFilesIgnored:
    """A yt-dlp.conf on the host or in the image must never reach argv.

    It could re-enable --exec, an external downloader, a cookie file, or
    -P/-o, the last of which would write media to disk.
    """

    def test_video_command_ignores_config_files(self) -> None:
        cmd = _build_video_command("https://www.youtube.com/watch?v=test", "best")
        assert "--ignore-config" in cmd

    def test_audio_command_ignores_config_files(self) -> None:
        cmd = _build_audio_command("https://www.youtube.com/watch?v=test")
        assert "--ignore-config" in cmd

    def test_ignore_config_precedes_every_other_option(self) -> None:
        # yt-dlp parses argv left to right, so the flag has to sit ahead of
        # anything a config file could contradict.
        for cmd in (
            _build_video_command("https://www.youtube.com/watch?v=test", "best"),
            _build_audio_command("https://www.youtube.com/watch?v=test"),
        ):
            assert cmd[0] == "yt-dlp"
            assert cmd[1] == "--ignore-config"


class TestExtractorsRestricted:
    """Only YouTube's own extractors may ever run.

    Without this, a youtube.com path that YoutubeTabIE declines falls
    through to GenericIE, which follows redirects off YouTube entirely
    and would turn /api/download into an open proxy given any open
    redirect on the allow-listed hosts.
    """

    def test_base_opts_name_the_allowed_extractors(self) -> None:
        assert _base_opts()["allowed_extractors"] == ["youtube.*"]

    def test_both_commands_restrict_extractors(self) -> None:
        for cmd in (
            _build_video_command("https://www.youtube.com/watch?v=test", "best"),
            _build_audio_command("https://www.youtube.com/watch?v=test"),
        ):
            assert "--use-extractors" in cmd
            assert cmd[cmd.index("--use-extractors") + 1] == "youtube.*"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLtest123456789",
            "https://www.youtube.com/playlist?list=PLtest123456789",
            "https://www.youtube.com/shorts/abcdefghijk",
            "https://www.youtube.com/clip/UgkxABC123",
            "https://www.youtube.com/@SomeChannel",
        ],
    )
    def test_real_youtube_urls_reach_a_permitted_extractor(self, url: str) -> None:
        # Guards against narrowing the allow-list until a legitimate URL
        # form has no extractor left: youtu.be links with a list
        # parameter are what YouTube's own share button produces from a
        # playlist, and they need YoutubeYtBe.
        assert _permitted_extractors_for(url), url

    def test_non_youtube_paths_still_have_no_extractor(self) -> None:
        # These fall through to generic, which follows redirects off
        # YouTube -- the reason the allow-list exists.
        for url in (
            "https://www.youtube.com/about/xyz",
            "https://www.youtube.com/t/terms",
        ):
            assert not _permitted_extractors_for(url), url

    def test_generic_extractor_is_not_allowed(self) -> None:
        for cmd in (
            _build_video_command("https://www.youtube.com/watch?v=test", "best"),
            _build_audio_command("https://www.youtube.com/watch?v=test"),
        ):
            assert "generic" not in cmd[cmd.index("--use-extractors") + 1]
        assert "generic" not in _base_opts()["allowed_extractors"]


class TestResolveVideoFormat:
    @pytest.mark.parametrize("quality", ["480", "720", "1080"])
    def test_bounded_quality_never_falls_back_to_unrestricted_best(
        self, quality: str
    ) -> None:
        spec = _resolve_video_format(quality)
        for fallback in spec.split("/"):
            assert f"height<={quality}" in fallback, (
                f"fallback {fallback!r} for quality {quality} would allow "
                "an unbounded best download"
            )

    def test_best_quality_falls_back_to_unrestricted_best(self) -> None:
        spec = _resolve_video_format("best")
        assert spec.endswith("/best")

    def test_unknown_quality_falls_back_to_best_spec(self) -> None:
        assert _resolve_video_format("garbage") == _resolve_video_format("best")


class TestFinalizeProcess:
    def _make_process(self, returncode: int) -> MagicMock:
        process = MagicMock()
        process.poll.return_value = returncode  # already exited
        process.returncode = returncode
        process.stdout = None
        return process

    def test_logs_error_when_process_exits_non_zero(self) -> None:
        process = self._make_process(returncode=1)

        with patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("yt-dlp", process, drainer=None)

        mock_logger.error.assert_called_once_with("%s exited with code %s", "yt-dlp", 1)

    def test_does_not_log_error_for_clean_exit(self) -> None:
        process = self._make_process(returncode=0)

        with patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("ffmpeg", process, drainer=None)

        mock_logger.error.assert_not_called()

    def test_does_not_log_error_for_sigkill(self) -> None:
        # We send SIGKILL ourselves during teardown, so it is expected.
        process = self._make_process(returncode=-signal.SIGKILL)

        with patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("yt-dlp", process, drainer=None)

        mock_logger.error.assert_not_called()

    def test_handles_none_process_silently(self) -> None:
        # The streaming pipelines call _finalize_process from outer finally
        # blocks where the subprocess may have failed to initialize.
        with patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("yt-dlp", process=None, drainer=None)

        mock_logger.error.assert_not_called()
