"""YouTube service layer wrapping yt-dlp for metadata extraction and streaming."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
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
# Teardown runs while a response is being torn down, on a worker
# thread either way, so it must not be able to block indefinitely.
PROCESS_EXIT_TIMEOUT = 5.0
MAX_PLAYLIST_SIZE = 200
# How many stderr lines to keep so a failed subprocess can explain itself
# in the error surfaced to the caller. yt-dlp puts the reason on the last
# line or two; the rest is progress noise.
STDERR_TAIL_LINES = 10
# Anchored on the subject, because a bare "not available" also matches
# two things that are not a missing video: yt-dlp's "Requested format
# is not available" (the video exists, the format does not) and
# "Impersonate target is not available" (a dependency missing on our
# side). Both were being reported to callers as 404.
#
# The wordings differ more than the source strings suggest -- grepping
# yt-dlp finds "Video unavailable", but a live request for a missing
# video returns "This video is unavailable". Add observed phrasings
# here rather than loosening the anchor.
UNAVAILABLE_MARKERS = (
    "private video",
    "video unavailable",
    "video is unavailable",
    "video is not available",
    "video has been removed",
    "not available in your region",
)
# Checked before UNAVAILABLE_MARKERS: the video is there, the quality
# the caller asked for is not.
FORMAT_UNAVAILABLE_MARKERS = ("requested format is not available",)
# yt-dlp says one of these when no permitted extractor claims the URL.
# It happens for YouTube paths that are not media -- /about, /t/terms,
# a mistyped path -- so it is the caller's input, not a server fault.
UNSUPPORTED_URL_MARKERS = ("no suitable extractor", "unsupported url")
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
# The [ext=mp4] filters in _resolve_video_format constrain the container,
# not the codec, and YouTube serves AV1 in mp4. yt-dlp's default sort
# ranks AV1 first, so without this every tier picked AV1 whenever a video
# had it. Far fewer hardware decoders handle AV1 than H.264: a car head
# unit that plays H.264 MP4 failed on these downloads (#112). This is the
# sort from yt-dlp's own "-t mp4" preset: prefer H.264 and AAC, fall back
# to other codecs only when no H.264 rendition fits the requested height
# ceiling (the [height<=N] filter applies before this sort). YouTube rarely
# offers H.264 above 1080p, so "best" usually tops out there; that trade
# was chosen for compatibility.
VIDEO_FORMAT_SORT = "vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac"
# The audio half of an mp4 download. Selected by its own yt-dlp process,
# so it cannot share the video's height ceiling -- and does not need one.
# The progressive fallback only matters for videos with no separate audio
# format; ffmpeg then takes just its audio track.
AUDIO_TRACK_FORMAT = "bestaudio[ext=m4a]/best"


class YouTubeError(Exception):
    """Base exception for YouTube service errors."""


class VideoNotFoundError(YouTubeError):
    """Raised when a video cannot be found or is unavailable."""


class InvalidURLError(YouTubeError):
    """Raised when the provided URL is not a valid YouTube URL."""


class FormatUnavailableError(YouTubeError):
    """Raised when the video exists but not in the requested quality.

    Like :class:`UnsupportedURLError`, this describes what the caller
    asked for rather than a server failure, so callers map it to a 4xx.
    """


class UnsupportedURLError(YouTubeError):
    """Raised when the URL is on YouTube but is not media we can fetch.

    Like :class:`PlaylistTooLargeError`, this describes the caller's
    input rather than a server failure, so callers map it to a 4xx.
    """


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


@dataclass
class _SupervisedProcess:
    """A registered subprocess and the thread draining its stderr.

    Mutable because the drainer is attached after registration: see
    ``DownloadProcesses.register``.
    """

    name: str
    process: subprocess.Popen[bytes]
    drainer: threading.Thread | None


class DownloadProcesses:
    """Handle on the subprocesses behind one download stream.

    A stream's subprocesses are started inside the generator that yields
    its chunks, which normally also tears them down in its ``finally``.
    That is not enough on the client-disconnect path: Starlette wraps a
    sync iterator in ``iterate_in_threadpool``, which never calls
    ``close()`` on it, so a cancelled response abandons the generator
    instead of closing it and the ``finally`` runs only if the garbage
    collector happens to reach it -- measured at 13 of 31 disconnects,
    tens of seconds late, and not once within 90s against real yt-dlp.
    Every disconnect therefore left a yt-dlp and an ffmpeg downloading
    to nowhere. Registering them here gives the router something it can
    close from a BackgroundTask, which does run on that path.

    ``close`` is the single teardown entry point for both paths, and it
    holds a lock because the two can overlap: the background task runs
    on a threadpool worker while the generator may still be unwinding on
    another. Two teardowns interleaving is how a pid once got released
    between ``os.getpgid`` and ``os.killpg``, which sent SIGKILL to
    whatever group had inherited the number.
    """

    def __init__(self) -> None:
        self._entries: list[_SupervisedProcess] = []
        self._lock = threading.Lock()

    def register(
        self,
        name: str,
        process: subprocess.Popen[bytes],
        tail: deque[str],
    ) -> threading.Thread | None:
        """Take ownership of a process and start draining its stderr.

        Starting the drainer is done here, after the process is already
        recorded, because ``Thread.start()`` can raise and a process
        nobody has recorded is a process nobody can kill -- which is
        the leak this class exists to prevent. Leaving that ordering to
        the call sites is what let it regress once already.

        Returns the drainer so the caller can join it before reporting
        a non-zero exit. If ``close`` happens to run in the window
        before the drainer is attached, that thread is not joined; it is
        a daemon that ends at stderr EOF once the process is killed.
        """
        entry = _SupervisedProcess(name, process, None)
        with self._lock:
            self._entries.append(entry)
        entry.drainer = _start_stderr_drainer(name, process, tail)
        return entry.drainer

    def close(self) -> None:
        """Finalize every registered subprocess, newest first.

        Idempotent: entries are dropped as they are finalized, so the
        second caller of a racing pair finds nothing left to do.
        """
        with self._lock:
            pending, self._entries = self._entries, []
            for entry in reversed(pending):
                _finalize_process(entry.name, entry.process, entry.drainer)


def stream_download(
    url: str,
    format_type: str = "mp4",
    quality: str = "best",
    *,
    processes: DownloadProcesses | None = None,
) -> Generator[bytes]:
    """Stream a video download as chunks without writing to disk.

    Pipes a yt-dlp subprocess through an ffmpeg stage of our own
    straight to the caller, ensuring zero disk I/O on the server. yt-dlp
    skips post-processors in stdout mode and cannot write MP4 there, so
    ffmpeg converts to MP3 or remuxes to fragmented MP4.

    Args:
        url: YouTube video URL.
        format_type: Output format, either "mp4" or "mp3".
        quality: Quality selection (best, 1080, 720, 480).
        processes: Handle the caller can close to tear the pipeline
            down from outside the generator. Callers serving an HTTP
            response must pass one, because a client disconnect
            abandons this generator without closing it; see
            ``DownloadProcesses``. When omitted, the generator owns a
            private handle and cleans up after itself as before.

    Yields:
        Chunks of the downloaded media.

    Raises:
        InvalidURLError: If the URL is not a valid YouTube URL.
        YouTubeError: For download failures.
    """
    url = normalize_youtube_url(url)
    if processes is None:
        processes = DownloadProcesses()

    if format_type == "mp3":
        yield from _stream_mp3(url, processes)
    else:
        yield from _stream_video(url, quality, processes)


def _stream_video(
    url: str, quality: str, processes: DownloadProcesses
) -> Generator[bytes]:
    """Stream MP4 by merging separately downloaded video and audio.

    Each track is fetched by its own yt-dlp process and merged by our own
    ffmpeg, rather than letting one yt-dlp merge them. yt-dlp merges
    through FFmpegFD, which hands ffmpeg each URL for a single unchunked
    request; YouTube throttles those (a 14 MB video took 109 s instead of
    3.6 s) and the audio came out truncated (#115). Downloaded on their
    own, both tracks use yt-dlp's chunked HTTP downloader. It also sidesteps
    yt-dlp muxing MP4 to stdout, which it forces to MPEG-TS -- not the
    video/mp4 the response declares, and a container in which AV1 loses
    its codec identification (#108, #112).

    The video is resolved once and both downloads are fed the result. Each
    resolution runs deno to solve YouTube's player challenge, about 270 MiB
    at peak; resolving per track doubled that and pushed one 1080p download
    to 680 MiB in production. With --load-info-json the downloads skip
    extraction entirely.
    """
    info_json = _extract_info_json(_build_info_command(url), processes=processes)
    yield from _stream_through_ffmpeg(
        _build_video_commands(quality),
        _fragmented_mp4_command,
        processes=processes,
        stdin_payload=info_json,
    )


def _extract_info_json(cmd: list[str], *, processes: DownloadProcesses) -> bytes:
    """Run a yt-dlp ``-J`` command and return the info JSON it prints.

    The process is registered with ``processes`` like every other, and a
    non-zero exit is classified the same way, so an unavailable video
    still becomes a 404 before any response header is sent.
    """
    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise YouTubeError("yt-dlp is not installed or not in PATH.") from e
    drainer = processes.register("yt-dlp", process, tail)
    info_json = _stdout_of(process).read()
    # Same ordering as the pipeline: clean the group up (deno runs in it)
    # while the pid is still held, then reap.
    _await_exit_without_reaping(process)
    _kill_process_group(process)
    if process.wait() != 0:
        _raise_from_subprocess_failure("yt-dlp", process.returncode, tail, drainer)
    return info_json


def _stream_mp3(url: str, processes: DownloadProcesses) -> Generator[bytes]:
    """Stream MP3 by piping yt-dlp audio through ffmpeg for conversion.

    yt-dlp skips post-processors in stdout mode, so we pipe the raw
    audio stream through ffmpeg to convert to MP3.
    """
    yield from _stream_through_ffmpeg(
        [_build_audio_command(url)], _mp3_command, processes=processes
    )


def _mp3_command(inputs: Sequence[str]) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-i",
        inputs[0],
        # bestaudio/best can resolve to a progressive stream that still
        # carries video, which the mp3 muxer cannot take.
        "-vn",
        "-f",
        "mp3",
        "-ab",
        "192k",
        "-v",
        "quiet",
        "pipe:1",
    ]


def _fragmented_mp4_command(inputs: Sequence[str]) -> list[str]:
    """ffmpeg merging a video input and an audio input into fragmented MP4.

    A regular MP4 cannot be streamed: its moov index is written after the
    last sample, and moving it to the front means holding the whole file,
    on disk (forbidden here) or in memory (gigabytes). Fragmented MP4 puts
    an empty moov first and indexes each fragment as it goes, so it needs
    neither. default_base_moof is what MSE and CMAF players expect.
    """
    video, audio = inputs
    return [
        "ffmpeg",
        "-nostdin",
        # Without this, input ffmpeg cannot read from a pipe is logged and
        # then ignored: a progressive MP4 with its moov at the end (which the
        # single-format fallback could fetch) came out as a 1.3 KB file with
        # no samples, exit 0, and was served as a successful download. With
        # it the stage exits non-zero and the stream fails instead.
        "-xerror",
        "-i",
        video,
        "-i",
        audio,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-f",
        "mp4",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-v",
        "error",
        "pipe:1",
    ]


def _stream_through_ffmpeg(
    ytdlp_cmds: Sequence[list[str]],
    build_ffmpeg_cmd: Callable[[Sequence[str]], list[str]],
    *,
    processes: DownloadProcesses,
    stdin_payload: bytes | None = None,
) -> Generator[bytes]:
    """Stream the output of yt-dlp processes through an ffmpeg stage.

    Each yt-dlp writes to its own pipe, and ``build_ffmpeg_cmd`` gets one
    ffmpeg input per command, in order (``pipe:<fd>``); the command it
    returns must write ``pipe:1``. Every process is registered with
    ``processes`` so a disconnect can tear the pipeline down from outside
    the generator. ``stdin_payload``, when given, is written to each
    yt-dlp's stdin (the info JSON for ``--load-info-json -``).
    """
    sources: list[
        tuple[subprocess.Popen[bytes], deque[str], threading.Thread | None]
    ] = []
    ffmpeg_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    try:
        try:
            for ytdlp_cmd in ytdlp_cmds:
                ytdlp_proc = subprocess.Popen(
                    ytdlp_cmd,
                    stdin=subprocess.DEVNULL
                    if stdin_payload is None
                    else subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
                # Registered before ffmpeg starts: if ffmpeg turns out to
                # be missing, this is the only handle that can still reach
                # yt-dlp.
                drainer = processes.register("yt-dlp", ytdlp_proc, tail)
                sources.append((ytdlp_proc, tail, drainer))
                if stdin_payload is not None:
                    _feed_stdin(ytdlp_proc, stdin_payload)

            input_fds = [_stdout_of(proc).fileno() for proc, _, _ in sources]
            ffmpeg_proc = subprocess.Popen(
                build_ffmpeg_cmd([f"pipe:{fd}" for fd in input_fds]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=input_fds,
                start_new_session=True,
            )
            ffmpeg_drainer = processes.register("ffmpeg", ffmpeg_proc, ffmpeg_tail)
        except FileNotFoundError as e:
            raise YouTubeError(
                "yt-dlp or ffmpeg is not installed or not in PATH."
            ) from e

        # Close our handles on the yt-dlp pipes so ffmpeg owns them; a
        # yt-dlp receives SIGPIPE if ffmpeg exits early.
        for proc, _, _ in sources:
            if proc.stdout:
                proc.stdout.close()

        if ffmpeg_proc.stdout is None:
            raise YouTubeError("Failed to open ffmpeg stdout pipe.")
        # One chunk is held back until the exit statuses are known. The
        # router sends the 200 as soon as the first chunk arrives, so a
        # stage that fails after writing only a header -- ffmpeg emits
        # about 1.3 KB of ftyp/moov before it discovers an unreadable
        # input -- used to reach the client as an aborted video/mp4.
        # Held back, a failure within the first chunk surfaces before the
        # headers and becomes an error response instead.
        held = b""
        while True:
            chunk = ffmpeg_proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            if held:
                yield held
            held = chunk

        # A non-zero exit below means the download failed; without the
        # checks the generator would end normally and the caller would
        # serve a successful-looking but truncated response.
        #
        # Clean each group up before reaping it: anything a process
        # spawned is only reachable while its pid is still held, and once
        # the pid is released there is no safe way to reach it.
        for proc in (*(proc for proc, _, _ in sources), ffmpeg_proc):
            _await_exit_without_reaping(proc)
            _kill_process_group(proc)

        # yt-dlp is checked first: when it fails, ffmpeg's own non-zero
        # exit is only a consequence of receiving a truncated stream,
        # and yt-dlp's stderr carries the reason worth reporting.
        for proc, tail, drainer in sources:
            if proc.wait() != 0:
                _raise_from_subprocess_failure("yt-dlp", proc.returncode, tail, drainer)
        if ffmpeg_proc.wait() != 0:
            _raise_from_subprocess_failure(
                "ffmpeg", ffmpeg_proc.returncode, ffmpeg_tail, ffmpeg_drainer
            )
        if held:
            yield held
    finally:
        # Reverse registration order, so the pipeline comes down from
        # its consumer end -- the order this block used before teardown
        # moved behind a single locked entry point.
        processes.close()


def _stdout_of(process: subprocess.Popen[bytes]) -> IO[bytes]:
    if process.stdout is None:
        raise YouTubeError("Failed to open yt-dlp stdout pipe.")
    return process.stdout


def _feed_stdin(process: subprocess.Popen[bytes], payload: bytes) -> None:
    """Write ``payload`` to the process's stdin and close it.

    Written in full before anything is read from stdout: yt-dlp's
    --load-info-json reads all of its input before it writes a byte, so
    the pipe drains as it fills. A process that dies first raises
    BrokenPipeError, which is left to its exit status to report.
    """
    if process.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError):
        process.stdin.write(payload)
    with contextlib.suppress(BrokenPipeError):
        process.stdin.close()


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
    if _is_unsupported_url_message(detail):
        raise UnsupportedURLError(detail)
    if _is_format_unavailable_message(detail):
        raise FormatUnavailableError(detail)
    if _is_unavailable_message(detail):
        raise VideoNotFoundError(f"Video is unavailable: {detail}")
    raise YouTubeError(f"{name} failed: {detail}")


def _await_exit_without_reaping(process: subprocess.Popen[bytes]) -> None:
    """Block until the subprocess exits, leaving it unreaped.

    WNOWAIT keeps the child in its zombie state, which keeps its pid
    allocated. That is what makes the group cleanup that follows safe
    *and* effective: while the pid is held it cannot have been reused,
    so os.getpgid still identifies our group, and anything the child
    spawned is still in it. Reaping first -- as Popen.wait does --
    releases the pid, and then teardown can neither find the group nor
    trust the number it was given.

    Signals sent to a zombie are discarded, so the exit status the
    caller reads afterwards is the one the child actually produced.
    """
    with contextlib.suppress(ChildProcessError):
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)


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
    # returncode, not poll(): reading the attribute tells us whether
    # Popen has already reaped the child, while poll() would do the
    # reaping itself and release the pid we are about to look up.
    #
    # Once reaped there is nothing left to signal -- the callers that
    # reap do the group cleanup first, while the pid is still held --
    # and the number may already belong to someone else, so signalling
    # it could reach an unrelated process group. Still None means
    # nobody has waited on this child, which is the disconnect path,
    # and there the group does need taking down.
    if process.returncode is None:
        _kill_process_group(process)
    if process.stdout:
        with contextlib.suppress(Exception):
            process.stdout.close()
    try:
        process.wait(timeout=PROCESS_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.error("%s did not exit within %ss", name, PROCESS_EXIT_TIMEOUT)
        return
    if drainer is not None:
        drainer.join(timeout=STDERR_DRAIN_TIMEOUT)
    # Treat SIGKILL as a clean teardown we initiated; everything else is
    # an unexpected failure that operators need to see in the logs.
    if process.returncode not in (0, -signal.SIGKILL):
        logger.error("%s exited with code %s", name, process.returncode)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the subprocess along with anything it spawned.

    yt-dlp starts processes of its own -- deno to solve YouTube's player
    challenges, ffmpeg whenever it merges formats or fetches a segmented
    one -- so killing only the direct child can leave a grandchild behind,
    ffmpeg holding HTTPS connections with no socket timeout of its own.
    Every subprocess here is started with ``start_new_session=True``, so
    its process group id equals its pid.

    Two things here are load-bearing. Do not remove either.

    ``os.getpgid`` is not a lookup for convenience: it is the proof
    that the pid is still ours to signal. It succeeds while the child
    runs and while it is a zombie -- in both states the pid is still
    held and cannot have been reused -- and raises ProcessLookupError
    once the child has been reaped. Passing ``process.pid`` straight to
    killpg instead skips that check and can signal whatever process
    group has since inherited the number.

    The ``pgid <= 1`` refusal is the backstop, because killpg is
    kill(-pgid): killpg(0) signals *our own* process group, and
    killpg(1) is kill(-1), which signals every process this user owns.
    A group we created can never be 0 or 1.
    """
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError, PermissionError:
        # Already reaped, or not ours: there is nothing safe to signal.
        return
    if pgid <= 1:
        logger.error("refusing to signal process group %s", pgid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


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
    return _build_ytdlp_command([url], "bestaudio/best")


def _build_info_command(url: str) -> list[str]:
    """yt-dlp command that resolves a video once and prints its info JSON."""
    return [*_YTDLP_BASE_ARGS, "-J", url]


def _build_video_commands(quality: str) -> tuple[list[str], list[str]]:
    """yt-dlp commands for the video and the audio track, in that order.

    Both read the info JSON from stdin (--load-info-json -) instead of
    resolving the video again; see _stream_video. Each selects a single
    format, so neither yt-dlp merges anything.
    """
    from_info = ["--load-info-json", "-"]
    sort = ["-S", VIDEO_FORMAT_SORT]
    return (
        _build_ytdlp_command(from_info, _resolve_video_format(quality), sort),
        _build_ytdlp_command(from_info, AUDIO_TRACK_FORMAT, sort),
    )


# --use-extractors still matters with --load-info-json: when a stored
# format fails to download, yt-dlp re-extracts from the info's
# webpage_url, and that must stay on YouTube's extractors too.
_YTDLP_BASE_ARGS = (
    "yt-dlp",
    "--ignore-config",
    "--use-extractors",
    ",".join(ALLOWED_EXTRACTORS),
    "--no-playlist",
    "--quiet",
    "--no-warnings",
    "--no-cache-dir",
    "--socket-timeout",
    str(SOCKET_TIMEOUT),
)


def _build_ytdlp_command(
    source: Sequence[str], format_spec: str, extra: Sequence[str] = ()
) -> list[str]:
    """yt-dlp command streaming one format of ``source`` to stdout.

    ``source`` is either the URL alone or the arguments that read an info
    JSON instead (``--load-info-json -``).
    """
    return [*_YTDLP_BASE_ARGS, "-f", format_spec, *extra, "-o", "-", *source]


def _resolve_video_format(quality: str) -> str:
    """Build the yt-dlp format spec for the video track of a quality.

    Each height-bounded entry keeps the height ceiling on every fallback
    so that requesting 480p never silently downloads 1080p when the
    requested resolution is unavailable. The "best" tier has no ceiling
    so it falls all the way back to whatever yt-dlp can produce. The
    progressive fallbacks carry audio too; ffmpeg only takes their video.
    """
    best_video = "bestvideo[ext=mp4]"
    quality_map = {
        "best": f"{best_video}/best[ext=mp4]/best",
        "1080": f"{best_video}[height<=1080]/best[height<=1080]",
        "720": f"{best_video}[height<=720]/best[height<=720]",
        "480": f"{best_video}[height<=480]/best[height<=480]",
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


def _is_format_unavailable_message(message: str) -> bool:
    """Whether an upstream error text means "not in that quality"."""
    lowered = message.lower()
    return any(marker in lowered for marker in FORMAT_UNAVAILABLE_MARKERS)


def _is_unsupported_url_message(message: str) -> bool:
    """Whether an upstream error text means "no extractor wanted this"."""
    lowered = message.lower()
    return any(marker in lowered for marker in UNSUPPORTED_URL_MARKERS)


def _raise_from_download_error(e: yt_dlp.utils.DownloadError) -> None:
    if _is_unsupported_url_message(str(e)):
        raise UnsupportedURLError(str(e)) from e
    if _is_format_unavailable_message(str(e)):
        raise FormatUnavailableError(str(e)) from e
    if _is_unavailable_message(str(e)):
        raise VideoNotFoundError(f"Video is unavailable: {e}") from e
    raise YouTubeError(f"Download error: {e}") from e


def _sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", name)
    sanitized = sanitized.strip(". ")
    return sanitized[:200] if sanitized else "download"
