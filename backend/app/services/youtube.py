"""YouTube service layer wrapping yt-dlp for metadata extraction and streaming."""

from __future__ import annotations

import contextlib
import logging
import re
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Generator
from typing import IO, Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

import yt_dlp

from app.schemas.video import (
    PlaylistEntry,
    PlaylistInfo,
    VideoFormat,
    VideoInfo,
)

logger = logging.getLogger(__name__)

# Scheme and host are case-insensitive per RFC 3986 section 3.1/3.2.2,
# so a pasted "https://WWW.YouTube.com/..." is a valid YouTube URL. The
# trailing "/" is load-bearing: it is what stops youtube.com@evil.com
# and youtube.com.evil.com from matching, so do not relax it.
YOUTUBE_URL_PATTERN = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/",
    re.IGNORECASE,
)

SOCKET_TIMEOUT = 30
CHUNK_SIZE = 65536
STDERR_DRAIN_TIMEOUT = 2.0
MAX_PLAYLIST_SIZE = 200
# How many stderr lines to keep so a failed subprocess can explain itself
# in the error surfaced to the caller. yt-dlp puts the reason on the last
# line or two; the rest is progress noise.
STDERR_TAIL_LINES = 10
UNAVAILABLE_MARKERS = ("private", "unavailable", "not available")
# The host allow-list in YOUTUBE_URL_PATTERN validates the string the
# caller sent; it cannot constrain where yt-dlp goes next. Paths that
# YoutubeTabIE declines (about, t/terms, signin, results, ...) fall
# through to GenericIE, which fetches the URL and re-dispatches on the
# final URL after redirects -- youtube.com/about/xyz ends up requesting
# about.youtube. Naming the extractors keeps every request on YouTube.
#
# These are regexes matched against extractor names, not literal names
# (yt_dlp/YoutubeDL.py: "allowed_extractors: List of regexes to match
# against extractor names"). Listing "youtube" and "youtube:tab" matched
# exactly two of the twenty YouTube extractors, so share links such as
# youtu.be/<id>?list=<id> (YoutubeYtBe) and youtube.com/clip/<id>
# (youtube:clip) were rejected outright. "youtube.*" covers all twenty
# and still excludes generic, which is the one that leaves YouTube.
ALLOWED_EXTRACTORS = ("youtube.*",)


class YouTubeError(Exception):
    """Base exception for YouTube service errors."""


class VideoNotFoundError(YouTubeError):
    """Raised when a video cannot be found or is unavailable."""


class InvalidURLError(YouTubeError):
    """Raised when the provided URL is not a valid YouTube URL."""


class PlaylistTooLargeError(YouTubeError):
    """Raised when a playlist has more entries than the service serves.

    This is a property of the caller's input, not a server failure, so
    callers map it to a 4xx.
    """


def validate_youtube_url(url: str) -> None:
    """Validate that a URL points to YouTube.

    Callers that go on to hand the URL to yt-dlp should use
    :func:`normalize_youtube_url` instead, which validates and also
    fixes up the casing yt-dlp is picky about.

    Args:
        url: The URL to validate.

    Raises:
        InvalidURLError: If the URL is not a valid YouTube URL.
    """
    if not YOUTUBE_URL_PATTERN.match(url):
        raise InvalidURLError("URL must be a valid YouTube URL.")


def normalize_youtube_url(url: str) -> str:
    """Validate a YouTube URL and lowercase its scheme and host.

    Our pattern accepts any casing, because scheme and host are
    case-insensitive per RFC 3986 sections 3.1 and 3.2.2. yt-dlp's
    extractor patterns are not: "HTTPS://www.youtube.com/watch?v=x",
    "https://YOUTU.BE/x" and "https://WWW.YouTube.com/playlist?list=x"
    all passed validation and then failed with "No suitable extractor
    found", which the API reported as a 500. Lowercasing those two
    components makes the two layers agree.

    Only scheme and host are touched. Path and query keep their casing:
    video and playlist IDs are case-sensitive.

    Args:
        url: The URL to validate and normalise.

    Returns:
        The URL with a lowercase scheme and host.

    Raises:
        InvalidURLError: If the URL is not a valid YouTube URL.
    """
    validate_youtube_url(url)
    parts = urlsplit(url)
    # The pattern guarantees the host is followed immediately by "/",
    # so netloc holds no userinfo or port and is safe to lowercase whole.
    return urlunsplit(
        parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower())
    )


def extract_video_info(url: str) -> VideoInfo:
    """Extract metadata and available formats for a single video.

    Args:
        url: YouTube video URL.

    Returns:
        VideoInfo with metadata and available formats.

    Raises:
        InvalidURLError: If the URL is not a valid YouTube URL.
        VideoNotFoundError: If the video cannot be found.
        YouTubeError: For other extraction failures.
    """
    url = normalize_youtube_url(url)

    ydl_opts = _base_opts() | {"noplaylist": True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        _raise_from_download_error(e)
    except Exception as e:
        raise YouTubeError(f"Failed to extract video info: {e}") from e

    if info is None:
        raise VideoNotFoundError("Could not retrieve video information.")

    formats = _parse_formats(info.get("formats") or [])

    return VideoInfo(
        video_id=info.get("id", ""),
        title=info.get("title", "Unknown"),
        thumbnail=info.get("thumbnail", ""),
        duration=info.get("duration") or 0,
        uploader=info.get("uploader") or info.get("channel", "Unknown"),
        formats=formats,
    )


def extract_playlist_info(url: str) -> PlaylistInfo:
    """Extract metadata for a YouTube playlist.

    Args:
        url: YouTube playlist URL.

    Returns:
        PlaylistInfo with playlist metadata and video entries.

    Raises:
        InvalidURLError: If the URL is not a valid YouTube URL.
        VideoNotFoundError: If the playlist cannot be found.
        YouTubeError: For other extraction failures.
    """
    url = normalize_youtube_url(url)

    ydl_opts = _base_opts() | {
        "extract_flat": "in_playlist",
        "noplaylist": False,
        # Stop yt-dlp one entry past the limit instead of letting it
        # page through the whole playlist first. Without this the
        # server paid the full extraction cost of a several-thousand
        # video playlist only to reject it, on an endpoint any
        # unauthenticated caller can hit 30 times a minute.
        "playlistend": MAX_PLAYLIST_SIZE + 1,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        _raise_from_download_error(e)
    except Exception as e:
        raise YouTubeError(f"Failed to extract playlist info: {e}") from e

    if info is None:
        raise VideoNotFoundError("Could not retrieve playlist information.")

    raw_entries = info.get("entries") or []
    if len(raw_entries) > MAX_PLAYLIST_SIZE:
        raise PlaylistTooLargeError(
            f"This playlist has more than {MAX_PLAYLIST_SIZE} videos. "
            "Open a smaller playlist, or download the videos individually."
        )
    entries = [
        PlaylistEntry(
            video_id=e.get("id", ""),
            title=e.get("title", "Unknown"),
            duration=e.get("duration") or 0,
            thumbnail=e.get("thumbnail")
            or (e.get("thumbnails") or [{}])[0].get("url", ""),
        )
        for e in raw_entries
        if e is not None
    ]

    return PlaylistInfo(
        playlist_id=info.get("id", ""),
        title=info.get("title", "Unknown"),
        uploader=info.get("uploader") or info.get("channel", "Unknown"),
        video_count=len(entries),
        entries=entries,
    )


def stream_download(
    url: str,
    format_type: str = "mp4",
    quality: str = "best",
) -> Generator[bytes]:
    """Stream a video download as chunks without writing to disk.

    Uses yt-dlp subprocess to pipe output directly to the caller,
    ensuring zero disk I/O on the server. For MP3, pipes yt-dlp
    through ffmpeg for format conversion since yt-dlp skips
    post-processors in stdout mode.

    Args:
        url: YouTube video URL.
        format_type: Output format, either "mp4" or "mp3".
        quality: Quality selection (best, 1080, 720, 480).

    Yields:
        Chunks of the downloaded media.

    Raises:
        InvalidURLError: If the URL is not a valid YouTube URL.
        YouTubeError: For download failures.
    """
    url = normalize_youtube_url(url)

    if format_type == "mp3":
        yield from _stream_mp3(url)
    else:
        yield from _stream_video(url, quality)


def _stream_video(url: str, quality: str) -> Generator[bytes]:
    cmd = _build_video_command(url, quality)
    yield from _run_piped_process(cmd)


def _stream_mp3(url: str) -> Generator[bytes]:
    """Stream MP3 by piping yt-dlp audio through ffmpeg for conversion.

    yt-dlp skips post-processors in stdout mode, so we pipe the raw
    audio stream through ffmpeg to convert to MP3.
    """
    ytdlp_cmd = _build_audio_command(url)
    ffmpeg_cmd = [
        "ffmpeg",
        "-i",
        "pipe:0",
        "-f",
        "mp3",
        "-ab",
        "192k",
        "-v",
        "quiet",
        "pipe:1",
    ]

    # Initialize everything up-front so the outer finally can always
    # finalize whatever happens to be alive, regardless of where in the
    # pipeline setup raised.
    ytdlp_proc: subprocess.Popen[bytes] | None = None
    ffmpeg_proc: subprocess.Popen[bytes] | None = None
    ytdlp_drainer: threading.Thread | None = None
    ffmpeg_drainer: threading.Thread | None = None
    ytdlp_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    ffmpeg_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    try:
        try:
            ytdlp_proc = subprocess.Popen(
                ytdlp_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=ytdlp_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise YouTubeError(
                "yt-dlp or ffmpeg is not installed or not in PATH."
            ) from e

        # Close our handle on ytdlp's stdout so ffmpeg owns it; ytdlp will
        # receive SIGPIPE if ffmpeg exits early.
        if ytdlp_proc.stdout:
            ytdlp_proc.stdout.close()

        ytdlp_drainer = _start_stderr_drainer("yt-dlp", ytdlp_proc, ytdlp_tail)
        ffmpeg_drainer = _start_stderr_drainer("ffmpeg", ffmpeg_proc, ffmpeg_tail)

        if ffmpeg_proc.stdout is None:
            raise YouTubeError("Failed to open ffmpeg stdout pipe.")
        while True:
            chunk = ffmpeg_proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

        # yt-dlp is checked first: when it fails, ffmpeg's own non-zero
        # exit is only a consequence of receiving a truncated stream,
        # and yt-dlp's stderr carries the reason worth reporting.
        if ytdlp_proc.wait() != 0:
            _raise_from_subprocess_failure(
                "yt-dlp", ytdlp_proc.returncode, ytdlp_tail, ytdlp_drainer
            )
        if ffmpeg_proc.wait() != 0:
            _raise_from_subprocess_failure(
                "ffmpeg", ffmpeg_proc.returncode, ffmpeg_tail, ffmpeg_drainer
            )
    finally:
        _finalize_process("ffmpeg", ffmpeg_proc, ffmpeg_drainer)
        _finalize_process("yt-dlp", ytdlp_proc, ytdlp_drainer)


def _run_piped_process(
    cmd: list[str],
    name: str = "yt-dlp",
) -> Generator[bytes]:
    process: subprocess.Popen[bytes] | None = None
    drainer: threading.Thread | None = None
    stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    try:
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise YouTubeError("yt-dlp is not installed or not in PATH.") from e

        drainer = _start_stderr_drainer(name, process, stderr_tail)

        if process.stdout is None:
            raise YouTubeError("Failed to open stdout pipe.")
        while True:
            chunk = process.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

        # stdout hit EOF, so the process is on its way out. A non-zero
        # exit here means the download failed; without this the
        # generator would end normally and the caller would serve a
        # successful-looking but empty response.
        if process.wait() != 0:
            _raise_from_subprocess_failure(
                name, process.returncode, stderr_tail, drainer
            )
    finally:
        _finalize_process(name, process, drainer)


def _drain_stderr(name: str, stream: IO[bytes], tail: deque[str]) -> None:
    """Forward subprocess stderr lines to the logger, keeping the tail.

    ``tail`` is a bounded deque owned by the caller; it lets a failed
    subprocess explain itself in the error raised to the caller without
    holding the whole stderr stream in memory.
    """
    try:
        for line in iter(stream.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.warning("%s: %s", name, text)
                tail.append(text)
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _start_stderr_drainer(
    name: str, process: subprocess.Popen[bytes], tail: deque[str]
) -> threading.Thread | None:
    """Spawn a daemon thread that drains process.stderr into the logger."""
    if process.stderr is None:
        return None
    drainer = threading.Thread(
        target=_drain_stderr,
        args=(name, process.stderr, tail),
        daemon=True,
    )
    drainer.start()
    return drainer


def _raise_from_subprocess_failure(
    name: str,
    returncode: int,
    stderr_tail: deque[str],
    drainer: threading.Thread | None,
) -> NoReturn:
    """Turn a non-zero subprocess exit into the matching service error.

    Waits briefly for the stderr drainer so the tail is complete, then
    classifies the failure the same way the yt-dlp Python API path does
    (see ``_raise_from_download_error``).
    """
    if drainer is not None:
        drainer.join(timeout=STDERR_DRAIN_TIMEOUT)
    detail = " | ".join(stderr_tail) or f"exited with code {returncode}"
    if _is_unavailable_message(detail):
        raise VideoNotFoundError(f"Video is unavailable: {detail}")
    raise YouTubeError(f"{name} failed: {detail}")


def _finalize_process(
    name: str,
    process: subprocess.Popen[bytes] | None,
    drainer: threading.Thread | None,
) -> None:
    """Terminate, wait, join the stderr drainer, and log non-zero exits.

    Both arguments may be ``None`` so callers can always invoke this from
    a ``finally`` block, even when the process or drainer never made it
    past initialization.
    """
    if process is None:
        return
    if process.poll() is None:
        process.kill()
    if process.stdout:
        with contextlib.suppress(Exception):
            process.stdout.close()
    process.wait()
    if drainer is not None:
        drainer.join(timeout=STDERR_DRAIN_TIMEOUT)
    # Treat SIGKILL as a clean teardown we initiated; everything else is
    # an unexpected failure that operators need to see in the logs.
    if process.returncode not in (0, -signal.SIGKILL):
        logger.error("%s exited with code %s", name, process.returncode)


def build_download_filename(
    title: str | None,
    format_type: str = "mp4",
) -> str:
    """Build a download filename from a video title.

    Args:
        title: Video title (already known by caller). Falls back to
            "download" if None or empty.
        format_type: Output format (mp4 or mp3).

    Returns:
        Sanitized filename with appropriate extension.
    """
    name = _sanitize_filename(title) if title else "download"
    ext = "mp3" if format_type == "mp3" else "mp4"
    return f"{name}.{ext}"


# Both subprocess commands below pass --ignore-config. A yt-dlp.conf found
# anywhere on yt-dlp's search path -- next to the binary, ~/.config/yt-dlp,
# ~/.yt-dlp, /etc/yt-dlp -- is merged into argv by its CLI entry point, and
# could re-enable --exec, an external downloader, a cookie file, or -P/-o.
# The last of those would put media on disk and break the no-disk-I/O
# guarantee this service is built around. The /api/info path is unaffected:
# it drives yt-dlp through the Python API, which never reads those files.
def _build_audio_command(url: str) -> list[str]:
    return [
        "yt-dlp",
        "--ignore-config",
        "--use-extractors",
        ",".join(ALLOWED_EXTRACTORS),
        "--no-playlist",
        "-f",
        "bestaudio/best",
        "-o",
        "-",
        "--quiet",
        "--no-warnings",
        "--no-cache-dir",
        "--socket-timeout",
        str(SOCKET_TIMEOUT),
        url,
    ]


def _build_video_command(url: str, quality: str) -> list[str]:
    format_spec = _resolve_video_format(quality)
    return [
        "yt-dlp",
        "--ignore-config",
        "--use-extractors",
        ",".join(ALLOWED_EXTRACTORS),
        "--no-playlist",
        "-f",
        format_spec,
        "-o",
        "-",
        "--merge-output-format",
        "mp4",
        "--quiet",
        "--no-warnings",
        "--no-cache-dir",
        "--socket-timeout",
        str(SOCKET_TIMEOUT),
        url,
    ]


def _resolve_video_format(quality: str) -> str:
    """Build the yt-dlp format spec for a requested quality.

    Each height-bounded entry keeps the height ceiling on every fallback
    so that requesting 480p never silently downloads 1080p when the
    requested resolution is unavailable. The "best" tier has no ceiling
    so it falls all the way back to whatever yt-dlp can produce.
    """
    best_video = "bestvideo[ext=mp4]"
    best_audio = "bestaudio[ext=m4a]"
    best_combined_fallback = "best[ext=mp4]/best"
    quality_map = {
        "best": f"{best_video}+{best_audio}/{best_combined_fallback}",
        "1080": f"{best_video}[height<=1080]+{best_audio}/best[height<=1080]",
        "720": f"{best_video}[height<=720]+{best_audio}/best[height<=720]",
        "480": f"{best_video}[height<=480]+{best_audio}/best[height<=480]",
    }
    return quality_map.get(quality, quality_map["best"])


def _base_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "allowed_extractors": list(ALLOWED_EXTRACTORS),
        # Disable yt-dlp's player JS cache so the service writes nothing
        # to ~/.cache/yt-dlp at runtime.
        "cachedir": False,
    }


def _parse_formats(raw_formats: list[dict[str, Any]]) -> list[VideoFormat]:
    seen_qualities: set[str] = set()
    formats: list[VideoFormat] = []

    for f in raw_formats:
        height = f.get("height")
        has_video = f.get("vcodec", "none") != "none"
        has_audio = f.get("acodec", "none") != "none"

        if has_video and height:
            quality = f"{height}p"
        elif has_audio and not has_video:
            quality = "audio"
        else:
            continue

        key = f"{quality}-{f.get('ext', '')}"
        if key in seen_qualities:
            continue
        seen_qualities.add(key)

        filesize = f.get("filesize") or f.get("filesize_approx")

        formats.append(
            VideoFormat(
                format_id=f.get("format_id", ""),
                ext=f.get("ext", ""),
                quality=quality,
                has_video=has_video,
                has_audio=has_audio,
                filesize_approx=filesize,
            )
        )

    return formats


def _is_unavailable_message(message: str) -> bool:
    """Whether an upstream error text means "this video is not there"."""
    lowered = message.lower()
    return any(marker in lowered for marker in UNAVAILABLE_MARKERS)


def _raise_from_download_error(e: yt_dlp.utils.DownloadError) -> None:
    if _is_unavailable_message(str(e)):
        raise VideoNotFoundError(f"Video is unavailable: {e}") from e
    raise YouTubeError(f"Download error: {e}") from e


def _sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", name)
    sanitized = sanitized.strip(". ")
    return sanitized[:200] if sanitized else "download"
