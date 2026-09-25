"""Unit tests for the YouTube service layer."""

import signal
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from app.services.youtube import (
    DownloadProcesses,
    InvalidURLError,
    VideoNotFoundError,
    _base_opts,
    _build_audio_command,
    _build_video_command,
    _finalize_process,
    _kill_process_group,
    _resolve_video_format,
    build_download_filename,
    extract_playlist_info,
    extract_video_info,
    normalize_youtube_url,
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


class TestNormalizeYoutubeUrl:
    """Casing yt-dlp rejects must be fixed before it sees the URL."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "HTTPS://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
            (
                "https://YOUTU.BE/dQw4w9WgXcQ",
                "https://youtu.be/dQw4w9WgXcQ",
            ),
            (
                "https://WWW.YouTube.com/playlist?list=PLabcDEF",
                "https://www.youtube.com/playlist?list=PLabcDEF",
            ),
            (
                "https://M.YouTube.com/watch?v=dQw4w9WgXcQ",
                "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
        ],
    )
    def test_lowercases_scheme_and_host(self, raw: str, expected: str) -> None:
        assert normalize_youtube_url(raw) == expected

    def test_preserves_case_in_path_and_query(self) -> None:
        # Video and playlist IDs are case-sensitive; touching them
        # would turn a valid URL into a 404.
        url = "https://www.youtube.com/watch?v=Ab_Cd-EfGhI&list=PLxYzAbC"
        assert normalize_youtube_url(url) == url

    def test_already_normalised_url_is_unchanged(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert normalize_youtube_url(url) == url

    def test_rejects_non_youtube_url(self) -> None:
        with pytest.raises(InvalidURLError):
            normalize_youtube_url("https://vimeo.com/12345")

    @pytest.mark.parametrize(
        "url",
        [
            "https://YOUTUBE.COM.EVIL.COM/watch?v=x",
            "https://youtube.com@evil.com/watch?v=x",
        ],
    )
    def test_lookalike_hosts_are_still_rejected(self, url: str) -> None:
        with pytest.raises(InvalidURLError):
            normalize_youtube_url(url)


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


class TestUnsupportedUrlClassification:
    """A YouTube URL with no extractor is bad input, not a server fault."""

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_no_suitable_extractor_raises_unsupported(
        self, mock_ydl_cls: MagicMock
    ) -> None:
        import yt_dlp

        from app.services.youtube import UnsupportedURLError

        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "ERROR: No suitable extractor found for URL "
            "https://www.youtube.com/about/xyz"
        )
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(UnsupportedURLError):
            extract_video_info("https://www.youtube.com/about/xyz")

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_unavailable_video_is_still_not_found(
        self, mock_ydl_cls: MagicMock
    ) -> None:
        # The more specific reason must not swallow this one.
        import yt_dlp

        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "ERROR: Video unavailable"
        )
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(VideoNotFoundError):
            extract_video_info("https://www.youtube.com/watch?v=private1")


class TestErrorMessageClassification:
    """Each upstream reason has to reach the status that matches it.

    The marker list is matched as substrings, so a phrase that is too
    loose silently reclassifies unrelated failures. A bare
    "not available" used to catch yt-dlp's format error and its
    impersonation-dependency error and report both as a missing video.
    All strings below are taken from yt-dlp's own sources.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: [youtube] abc: Private video. Sign in if you have access",
            "ERROR: [youtube] abc: Video unavailable",
            "ERROR: [youtube] abc: This video is not available.",
            # Observed from the running service, not the yt-dlp source:
            # a request for a missing video returns this wording, which
            # an earlier narrowing of the markers stopped matching.
            "ERROR: [youtube] AAAAAAAAAAA: This video is unavailable",
            "ERROR: [youtube] abc: This video has been removed for violating YouTube",
            "ERROR: This playlist is likely not available in your region.",
        ],
    )
    def test_a_missing_video_is_not_found(self, message: str) -> None:
        from app.services.youtube import _is_unavailable_message

        assert _is_unavailable_message(message)

    def test_a_missing_format_is_not_a_missing_video(self) -> None:
        # The video is there; the quality the caller asked for is not.
        from app.services.youtube import (
            _is_format_unavailable_message,
            _is_unavailable_message,
        )

        message = (
            "ERROR: [youtube] abc: Requested format is not available. "
            "Use --list-formats for a list of available formats"
        )

        assert _is_format_unavailable_message(message)
        assert not _is_unavailable_message(message)

    def test_a_missing_dependency_is_neither(self) -> None:
        # Impersonation needs curl_cffi on our side. Reporting this as
        # a missing video pointed the caller at their own URL.
        from app.services.youtube import (
            _is_format_unavailable_message,
            _is_unavailable_message,
            _is_unsupported_url_message,
        )

        message = "ERROR: Impersonate target is not available"

        assert not _is_unavailable_message(message)
        assert not _is_format_unavailable_message(message)
        assert not _is_unsupported_url_message(message)

    @patch("app.services.youtube.yt_dlp.YoutubeDL")
    def test_format_error_raises_its_own_type(self, mock_ydl_cls: MagicMock) -> None:
        import yt_dlp

        from app.services.youtube import FormatUnavailableError

        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "ERROR: Requested format is not available"
        )
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(FormatUnavailableError):
            extract_video_info("https://www.youtube.com/watch?v=abc")


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


def _video_format(format_id: str, vcodec: str, height: int, tbr: int) -> dict:
    return {
        "format_id": format_id,
        "url": f"https://example.invalid/{format_id}",
        "protocol": "https",
        "ext": "mp4",
        "vcodec": vcodec,
        "acodec": "none",
        "height": height,
        "width": height * 16 // 9,
        "tbr": tbr,
    }


AAC_AUDIO = {
    "format_id": "140",
    "url": "https://example.invalid/140",
    "protocol": "https",
    "ext": "m4a",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "abr": 128,
    "tbr": 128,
}
AV1_1440 = _video_format("av1-1440", "av01.0.12M.08", 1440, 5000)
AV1_1080 = _video_format("av1-1080", "av01.0.08M.08", 1080, 3000)
H264_1080 = _video_format("h264-1080", "avc1.640028", 1080, 4000)
AV1_480 = _video_format("av1-480", "av01.0.04M.08", 480, 800)
H264_480 = _video_format("h264-480", "avc1.4d401e", 480, 1000)


def _select_video_format(quality: str, formats: list[dict]) -> str:
    """Run yt-dlp's real format selection with the video command's -f and -S.

    Reading both values out of the command, rather than off the
    constants, is what ties this to the argv yt-dlp actually receives.
    """
    import yt_dlp

    cmd = _build_video_command("https://www.youtube.com/watch?v=test", quality)
    opts = {
        "quiet": True,
        "simulate": True,
        "format": cmd[cmd.index("-f") + 1],
        "format_sort": cmd[cmd.index("-S") + 1].split(","),
    }
    info = {
        "id": "test",
        "title": "test",
        "extractor": "youtube",
        "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=test",
        "formats": [dict(f) for f in formats],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.process_ie_result(info, download=False)["format_id"]


class TestVideoCodecPreference:
    """fmt=mp4 has to pick H.264 over AV1, which many players cannot decode.

    [ext=mp4] only constrains the container, and YouTube serves AV1 in
    mp4, so before the sort was added every tier picked AV1 whenever a
    video offered it (#112).
    """

    @pytest.mark.parametrize("quality", ["best", "1080"])
    def test_h264_is_chosen_over_av1(self, quality: str) -> None:
        formats = [AV1_1440, AV1_1080, H264_1080, H264_480, AAC_AUDIO]

        selected = _select_video_format(quality, formats)

        assert selected == "h264-1080+140"

    def test_bounded_tier_prefers_h264_within_its_height_ceiling(self) -> None:
        formats = [AV1_1440, AV1_1080, H264_1080, AV1_480, H264_480, AAC_AUDIO]

        selected = _select_video_format("480", formats)

        assert selected == "h264-480+140"

    def test_av1_is_still_served_when_there_is_no_h264(self) -> None:
        # A preference, not a filter: a video without H.264 must still
        # download rather than fail with "format not available".
        formats = [AV1_1440, AV1_1080, AAC_AUDIO]

        selected = _select_video_format("1080", formats)

        assert selected == "av1-1080+140"


class TestKillProcessGroup:
    """Guards on the one call that can take out unrelated processes.

    os.killpg(pgid) is kill(-pgid): killpg(0) hits the caller's own
    process group and killpg(1) is kill(-1), which signals every
    process the user owns. A teardown that dropped the os.getpgid()
    check and passed a reaped pid straight to killpg once wiped out
    every process of this uid on a developer machine.

    These tests never spawn or signal anything: os.killpg is patched.
    """

    def _process(self, pid: int = 4242) -> MagicMock:
        process = MagicMock()
        process.pid = pid
        return process

    def test_does_not_signal_a_reaped_process(self) -> None:
        # getpgid raising ProcessLookupError is how a released pid is
        # detected; the number may belong to someone else by now.
        with (
            patch("app.services.youtube.os.getpgid", side_effect=ProcessLookupError),
            patch("app.services.youtube.os.killpg") as mock_killpg,
        ):
            _kill_process_group(self._process())

        mock_killpg.assert_not_called()

    def test_does_not_signal_a_group_it_may_not_signal(self) -> None:
        with (
            patch("app.services.youtube.os.getpgid", side_effect=PermissionError),
            patch("app.services.youtube.os.killpg") as mock_killpg,
        ):
            _kill_process_group(self._process())

        mock_killpg.assert_not_called()

    @pytest.mark.parametrize("pgid", [0, 1, -1])
    def test_refuses_process_groups_that_are_never_ours(self, pgid: int) -> None:
        # start_new_session makes the group id the child's pid, so these
        # values can only mean the lookup returned something unrelated.
        with (
            patch("app.services.youtube.os.getpgid", return_value=pgid),
            patch("app.services.youtube.os.killpg") as mock_killpg,
            patch("app.services.youtube.logger") as mock_logger,
        ):
            _kill_process_group(self._process())

        mock_killpg.assert_not_called()
        mock_logger.error.assert_called_once()

    def test_teardown_skips_the_group_once_the_child_is_reaped(self) -> None:
        # The streaming paths clean the group up before reaping, so a
        # second attempt afterwards finds nothing of ours: the pid has
        # been released and the number may now belong to someone else.
        process = self._process()
        process.returncode = 0
        process.stdout = None

        with (
            patch("app.services.youtube.os.getpgid") as mock_getpgid,
            patch("app.services.youtube.os.killpg") as mock_killpg,
        ):
            _finalize_process("fake", process, drainer=None)

        mock_getpgid.assert_not_called()
        mock_killpg.assert_not_called()

    def test_teardown_signals_the_group_when_nobody_has_reaped(self) -> None:
        # The client-disconnect path: the generator is closed at the
        # yield, so no one waited on the child and the group is still
        # ours to take down.
        process = self._process()
        process.returncode = None
        process.stdout = None

        with (
            patch("app.services.youtube.os.getpgid", return_value=4242),
            patch("app.services.youtube.os.killpg") as mock_killpg,
        ):
            _finalize_process("fake", process, drainer=None)

        mock_killpg.assert_called_once_with(4242, signal.SIGKILL)

    def test_signals_the_group_of_a_live_child(self) -> None:
        with (
            patch("app.services.youtube.os.getpgid", return_value=4242),
            patch("app.services.youtube.os.killpg") as mock_killpg,
        ):
            _kill_process_group(self._process(pid=4242))

        mock_killpg.assert_called_once_with(4242, signal.SIGKILL)


class TestFinalizeProcess:
    def _make_process(self, returncode: int) -> MagicMock:
        process = MagicMock()
        process.poll.return_value = returncode  # already exited
        process.returncode = returncode
        process.stdout = None
        # A reaped child: getpgid on its pid raises, so teardown skips
        # the group signal. Patched per test via _reaped_group().
        process.pid = 4242
        return process

    def _reaped_group(self):
        return patch("app.services.youtube.os.getpgid", side_effect=ProcessLookupError)

    def test_logs_error_when_process_exits_non_zero(self) -> None:
        process = self._make_process(returncode=1)

        with self._reaped_group(), patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("yt-dlp", process, drainer=None)

        mock_logger.error.assert_called_once_with("%s exited with code %s", "yt-dlp", 1)

    def test_does_not_log_error_for_clean_exit(self) -> None:
        process = self._make_process(returncode=0)

        with self._reaped_group(), patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("ffmpeg", process, drainer=None)

        mock_logger.error.assert_not_called()

    def test_does_not_log_error_for_sigkill(self) -> None:
        # We send SIGKILL ourselves during teardown, so it is expected.
        process = self._make_process(returncode=-signal.SIGKILL)

        with self._reaped_group(), patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("yt-dlp", process, drainer=None)

        mock_logger.error.assert_not_called()

    def test_handles_none_process_silently(self) -> None:
        # The streaming pipelines call _finalize_process from outer finally
        # blocks where the subprocess may have failed to initialize.
        with patch("app.services.youtube.logger") as mock_logger:
            _finalize_process("yt-dlp", process=None, drainer=None)

        mock_logger.error.assert_not_called()


class TestDownloadProcesses:
    """The handle the router closes when a client disconnects.

    Teardown of a registered process goes through _finalize_process and
    _kill_process_group, so the guards on signalling live in
    TestKillProcessGroup and TestFinalizeProcess. What is pinned here is
    the bookkeeping those guards depend on: registration order,
    registration happening before anything that can fail, and doing
    nothing twice.
    """

    def _register(self, processes: DownloadProcesses, name: str) -> MagicMock:
        """Register a mock process with the drainer stubbed out.

        register() starts a real stderr drainer, which would read from a
        MagicMock's stderr and raise inside the thread.
        """
        process = MagicMock()
        with patch("app.services.youtube._start_stderr_drainer", return_value=None):
            processes.register(name, process, deque())
        return process

    def test_finalizes_in_reverse_registration_order(self) -> None:
        # A pipeline comes down from its consumer end, the order
        # _stream_mp3 used when it finalized ffmpeg before yt-dlp.
        finalized: list[str] = []
        processes = DownloadProcesses()
        self._register(processes, "yt-dlp")
        self._register(processes, "ffmpeg")

        with patch(
            "app.services.youtube._finalize_process",
            side_effect=lambda name, process, drainer: finalized.append(name),
        ):
            processes.close()

        assert finalized == ["ffmpeg", "yt-dlp"]

    def test_second_close_finalizes_nothing(self) -> None:
        # Both the generator's finally and the response background task
        # call close, and on a disconnect they can overlap. The second
        # one must not reach a process the first already reaped, whose
        # pid may by then belong to someone else.
        processes = DownloadProcesses()
        self._register(processes, "yt-dlp")

        with patch("app.services.youtube._finalize_process") as finalize:
            processes.close()
            processes.close()

        assert finalize.call_count == 1

    def test_close_without_registrations_does_nothing(self) -> None:
        # Reached whenever the first Popen raised: the generator's
        # finally still runs, with nothing registered to tear down.
        processes = DownloadProcesses()

        with patch("app.services.youtube._finalize_process") as finalize:
            processes.close()

        finalize.assert_not_called()

    def test_process_is_owned_even_if_its_drainer_cannot_start(self) -> None:
        # threading.Thread.start() raises RuntimeError when no thread
        # can be created. Recording the process only afterwards left it
        # with no owner, so the finally that calls close() found nothing
        # and the process ran on -- the very leak this class prevents.
        processes = DownloadProcesses()
        process = MagicMock()

        with (
            patch(
                "app.services.youtube._start_stderr_drainer",
                side_effect=RuntimeError("can't start new thread"),
            ),
            pytest.raises(RuntimeError),
        ):
            processes.register("yt-dlp", process, deque())

        with patch("app.services.youtube._finalize_process") as finalize:
            processes.close()

        finalize.assert_called_once_with("yt-dlp", process, None)

    def test_register_returns_the_drainer_for_the_caller_to_join(self) -> None:
        # The stream body joins it before reporting a non-zero exit, so
        # the stderr tail is complete in the error it raises.
        processes = DownloadProcesses()
        drainer = MagicMock()

        with patch("app.services.youtube._start_stderr_drainer", return_value=drainer):
            assert processes.register("yt-dlp", MagicMock(), deque()) is drainer
