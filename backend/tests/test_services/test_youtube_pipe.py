"""Integration tests for subprocess pipe orchestration.

These tests use a small Python program as a stand-in for yt-dlp so the
streaming pipeline can be exercised end-to-end without depending on
external services. They cover the orchestration layer that the
extensive mock-based unit tests in ``test_youtube.py`` cannot reach:
chunk assembly across read boundaries, stderr draining under load, and
binary-missing failure modes.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from app.services.youtube import (
    DownloadProcesses,
    UnsupportedURLError,
    VideoNotFoundError,
    YouTubeError,
    _build_video_commands,
    _finalize_process,
    _fragmented_mp4_command,
    _stream_through_ffmpeg,
    stream_download,
)

# Linux-only: the project ships as a Linux container and CI runs on
# ubuntu-latest.
PROC_STATUS = "/proc/{pid}/stat"


def _is_alive(pid: int) -> bool:
    """Whether pid exists and is not a zombie awaiting reaping."""
    try:
        with open(PROC_STATUS.format(pid=pid)) as stat:
            state = stat.read().rsplit(") ", 1)[1].split()[0]
    # ESRCH, not ENOENT, is what the kernel returns when the pid is
    # reaped between the lookup and the read. Either way it is gone.
    except FileNotFoundError, ProcessLookupError:
        return False
    return state != "Z"


def _python(source: str) -> list[str]:
    return [sys.executable, "-c", source]


# The ffmpeg stand-ins get their inputs the way ffmpeg does, as pipe:<fd>
# arguments; this reads all of them, in order, into `data`.
READ_INPUTS = (
    "import os, sys\n"
    "data = b''.join(\n"
    "    chunk\n"
    "    for arg in sys.argv[1:]\n"
    "    for chunk in iter(lambda: os.read(int(arg[5:]), 65536), b''))\n"
)


def _stage(source: str):
    """An ffmpeg stand-in: ``source`` runs with the inputs in its argv."""
    return lambda inputs: [*_python(source), *inputs]


# Copies each input to stdout as it arrives, so a stream that never ends
# still reaches the reader.
_passthrough = _stage(
    "import os, sys\n"
    "for arg in sys.argv[1:]:\n"
    "    while chunk := os.read(int(arg[5:]), 65536):\n"
    "        sys.stdout.buffer.write(chunk)\n"
    "        sys.stdout.buffer.flush()\n"
)


def _wait_until_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestStreamOutcomes:
    def test_drains_stderr_without_blocking_stdout(self) -> None:
        # Write enough stderr to overflow the default pipe buffer (~64
        # KiB on Linux) before producing any stdout. Without the stderr
        # drainer thread, the child would block on stderr and never
        # complete the stdout write, so the join would hang.
        cmd = [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.stderr.write('warn\\n' * 20000)\n"
            "sys.stderr.flush()\n"
            "sys.stdout.buffer.write(b'payload')\n",
        ]

        result = b"".join(
            _stream_through_ffmpeg([cmd], _passthrough, processes=DownloadProcesses())
        )

        assert result == b"payload"

    def test_raises_when_process_exits_non_zero_after_empty_stdout(self) -> None:
        # The failure mode that made an unavailable video look like a
        # successful download: no stdout, non-zero exit, generator ends.
        cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]

        with pytest.raises(YouTubeError, match="exited with code 1"):
            list(
                _stream_through_ffmpeg(
                    [cmd], _passthrough, processes=DownloadProcesses()
                )
            )

    def test_raises_when_process_exits_non_zero_after_partial_stdout(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            "import sys\nsys.stdout.buffer.write(b'partial')\nsys.exit(1)\n",
        ]

        with pytest.raises(YouTubeError):
            list(
                _stream_through_ffmpeg(
                    [cmd], _passthrough, processes=DownloadProcesses()
                )
            )

    def test_reports_unavailable_video_as_not_found(self) -> None:
        # yt-dlp writes the reason to stderr and exits non-zero; the
        # caller needs 404 rather than 500 for this case.
        cmd = [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.stderr.write('ERROR: Video unavailable\\n')\n"
            "sys.exit(1)\n",
        ]

        with pytest.raises(VideoNotFoundError, match="Video unavailable"):
            list(
                _stream_through_ffmpeg(
                    [cmd], _passthrough, processes=DownloadProcesses()
                )
            )

    def test_reports_unsupported_url_as_such(self) -> None:
        # What the CLI prints when the extractor allow-list refuses a
        # YouTube path that is not media.
        cmd = [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.stderr.write('ERROR: No suitable extractor found for URL x\\n')\n"
            "sys.exit(1)\n",
        ]

        with pytest.raises(UnsupportedURLError, match="No suitable extractor"):
            list(
                _stream_through_ffmpeg(
                    [cmd], _passthrough, processes=DownloadProcesses()
                )
            )

    def test_clean_exit_with_output_does_not_raise(self) -> None:
        cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok')"]

        assert (
            b"".join(
                _stream_through_ffmpeg(
                    [cmd], _passthrough, processes=DownloadProcesses()
                )
            )
            == b"ok"
        )


class TestStreamThroughFfmpeg:
    """The two-stage pipeline shared by the mp3 and mp4 paths."""

    def test_yields_the_second_stage_output_in_full(self) -> None:
        payload_size = 200_000
        producer = _python(
            f"import sys; sys.stdout.buffer.write(b'a' * {payload_size})"
        )

        result = b"".join(
            _stream_through_ffmpeg(
                [producer], _passthrough, processes=DownloadProcesses()
            )
        )

        assert result == b"a" * payload_size

    def test_raises_when_a_binary_is_missing(self) -> None:
        with pytest.raises(YouTubeError, match="not installed"):
            list(
                _stream_through_ffmpeg(
                    [["/no/such/binary"]], _passthrough, processes=DownloadProcesses()
                )
            )

    def test_reports_the_ffmpeg_stage_failing(self) -> None:
        producer = _python("import sys; sys.stdout.buffer.write(b'data')")
        failing_stage = _stage(
            READ_INPUTS
            + "sys.stderr.write('Invalid data found when processing input\\n')\n"
            "sys.exit(1)\n"
        )

        with pytest.raises(YouTubeError, match="ffmpeg failed: Invalid data"):
            list(
                _stream_through_ffmpeg(
                    [producer], failing_stage, processes=DownloadProcesses()
                )
            )

    def test_reports_yt_dlp_when_both_stages_fail(self) -> None:
        # ffmpeg failing on a truncated input is only a consequence;
        # the reason worth reporting is on yt-dlp's stderr.
        producer = _python(
            "import sys\nsys.stderr.write('ERROR: Video unavailable\\n')\nsys.exit(1)\n"
        )
        failing_stage = _stage(READ_INPUTS + "sys.exit(1)\n")

        with pytest.raises(VideoNotFoundError, match="Video unavailable"):
            list(
                _stream_through_ffmpeg(
                    [producer], failing_stage, processes=DownloadProcesses()
                )
            )

    def test_every_source_reaches_the_stage_in_order(self) -> None:
        video = _python("import sys; sys.stdout.buffer.write(b'video')")
        audio = _python("import sys; sys.stdout.buffer.write(b'audio')")

        result = b"".join(
            _stream_through_ffmpeg(
                [video, audio], _passthrough, processes=DownloadProcesses()
            )
        )

        assert result == b"videoaudio"

    def test_reports_the_second_source_failing(self) -> None:
        video = _python("import sys; sys.stdout.buffer.write(b'video')")
        audio = _python(
            "import sys\nsys.stderr.write('ERROR: Video unavailable\\n')\nsys.exit(1)\n"
        )

        with pytest.raises(VideoNotFoundError, match="Video unavailable"):
            list(
                _stream_through_ffmpeg(
                    [video, audio], _passthrough, processes=DownloadProcesses()
                )
            )


def _top_level_boxes(data: bytes, count: int) -> list[str]:
    """Names of the first ``count`` ISO BMFF boxes in ``data``."""
    names = []
    offset = 0
    while len(names) < count and offset + 8 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        names.append(data[offset + 4 : offset + 8].decode("latin-1"))
        if size < 8:
            break
        offset += size
    return names


# Skipped locally when ffmpeg is missing, but never in CI (GitHub sets
# CI=true): there a missing ffmpeg must fail these tests, not hide them.
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and not os.environ.get("CI"),
    reason="needs ffmpeg",
)


def _lavfi(source: str, codec: list[str]) -> list[str]:
    """Stands in for one yt-dlp: a two-second single-track stream."""
    return [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        source,
        *codec,
        "-f",
        "matroska",
        "pipe:1",
    ]


LAVFI_VIDEO = _lavfi("testsrc=duration=2:size=320x240:rate=25", ["-c:v", "mpeg4"])
LAVFI_AUDIO = _lavfi("sine=duration=2", ["-c:a", "aac"])


def _stream_types(data: bytes) -> list[str]:
    """Stream types ffmpeg finds in ``data``, e.g. ["video", "audio"].

    Parsed from ffmpeg's own input summary rather than ffprobe, so these
    tests need one binary, not two.
    """
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", "pipe:0", "-f", "null", "-"],
        input=data,
        capture_output=True,
        check=True,
    )
    # Only the input summary: the null output lists its streams too.
    input_summary = probe.stderr.split(b"Stream mapping:")[0]
    found = re.findall(rb"Stream #0:\d+\S*: (Video|Audio):", input_summary)
    return [kind.decode().lower() for kind in found]


def _audio_seconds(data: bytes) -> float:
    """How long the audio track in ``data`` runs, read by copying it out.

    The last ``time=`` in ffmpeg's progress is where the copied stream
    ended, so a truncated track reports less than its source.
    """
    copy = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", "pipe:0", "-map", "0:a", "-c", "copy"]
        + ["-f", "null", "-"],
        input=data,
        capture_output=True,
        check=True,
    )
    hours, minutes, seconds = re.findall(rb"time=(\d+):(\d+):(\d+\.\d+)", copy.stderr)[
        -1
    ]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# LAVFI_AUDIO's length, and how far the merged track may drift from it
# (AAC frames are 1024 samples, about 23 ms at 44.1 kHz).
LAVFI_AUDIO_SECONDS = 2.0
AUDIO_LENGTH_TOLERANCE = 0.1


class TestVideoIsMergedByOurOwnFfmpeg:
    """Video and audio are fetched separately and merged into fragmented MP4.

    A single yt-dlp merging them hands ffmpeg unchunked requests, which
    YouTube throttles and which truncated the audio (#115); it also forces
    MPEG-TS on stdout (#108).
    """

    @pytest.mark.parametrize("quality", ["best", "1080", "720", "480"])
    def test_each_yt_dlp_fetches_a_single_format(self, quality: str) -> None:
        commands = _build_video_commands(
            "https://www.youtube.com/watch?v=test", quality
        )

        for cmd in commands:
            assert "+" not in cmd[cmd.index("-f") + 1]
            assert "--merge-output-format" not in cmd

    def test_video_goes_through_the_remux_stage(self) -> None:
        with patch(
            "app.services.youtube._stream_through_ffmpeg", return_value=iter(())
        ) as pipeline:
            list(stream_download("https://www.youtube.com/watch?v=test", "mp4"))

        assert pipeline.call_args.args[1] is _fragmented_mp4_command

    @needs_ffmpeg
    def test_output_is_a_fragmented_mp4(self) -> None:
        with patch(
            "app.services.youtube._build_video_commands",
            return_value=(LAVFI_VIDEO, LAVFI_AUDIO),
        ):
            output = b"".join(
                stream_download("https://www.youtube.com/watch?v=test", "mp4")
            )

        assert _top_level_boxes(output, 3) == ["ftyp", "moov", "moof"]

    @needs_ffmpeg
    def test_output_carries_the_video_of_one_and_the_audio_of_the_other(
        self,
    ) -> None:
        with patch(
            "app.services.youtube._build_video_commands",
            return_value=(LAVFI_VIDEO, LAVFI_AUDIO),
        ):
            output = b"".join(
                stream_download("https://www.youtube.com/watch?v=test", "mp4")
            )

        assert _stream_types(output) == ["video", "audio"]

    @needs_ffmpeg
    def test_merged_audio_runs_the_full_length_of_its_source(self) -> None:
        # Guards our own pipeline cutting the audio short, the symptom of
        # #115. It cannot reproduce #115's cause: YouTube throttling an
        # unchunked request only happens against YouTube.
        with patch(
            "app.services.youtube._build_video_commands",
            return_value=(LAVFI_VIDEO, LAVFI_AUDIO),
        ):
            output = b"".join(
                stream_download("https://www.youtube.com/watch?v=test", "mp4")
            )

        assert _audio_seconds(output) == pytest.approx(
            LAVFI_AUDIO_SECONDS, abs=AUDIO_LENGTH_TOLERANCE
        )


def _progressive_mp4(path: pathlib.Path, *, moov_first: bool) -> pathlib.Path:
    """Write a short single-file MP4 with its moov at the front or the end."""
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            *(["-movflags", "+faststart"] if moov_first else []),
            str(path),
        ],
        check=True,
    )
    return path


def _cat(path: pathlib.Path) -> list[str]:
    # Stands in for yt-dlp handing over a single progressive format,
    # which it writes to stdout unchanged.
    return [
        sys.executable,
        "-c",
        f"import shutil, sys; shutil.copyfileobj(open({str(path)!r}, 'rb'), "
        "sys.stdout.buffer)",
    ]


@needs_ffmpeg
class TestProgressiveFallbackThroughTheRemux:
    """The single-format fallback reaches the remux stage as a plain MP4.

    ffmpeg can only read that from a pipe when the moov comes first.
    YouTube's progressive itag 18 was observed with moov first; the other
    layout has to fail loudly rather than pass as an empty success.
    """

    def test_moov_first_is_remuxed(self, tmp_path: pathlib.Path) -> None:
        source = _progressive_mp4(tmp_path / "head.mp4", moov_first=True)

        with patch(
            "app.services.youtube._build_video_commands",
            return_value=(_cat(source), _cat(source)),
        ):
            output = b"".join(
                stream_download("https://www.youtube.com/watch?v=test", "mp4")
            )

        assert _top_level_boxes(output, 3) == ["ftyp", "moov", "moof"]

    def test_moov_last_fails_instead_of_serving_an_empty_file(
        self, tmp_path: pathlib.Path
    ) -> None:
        source = _progressive_mp4(tmp_path / "tail.mp4", moov_first=False)

        with (
            patch(
                "app.services.youtube._build_video_commands",
                return_value=(_cat(source), _cat(source)),
            ),
            pytest.raises(YouTubeError, match="ffmpeg failed"),
        ):
            b"".join(stream_download("https://www.youtube.com/watch?v=test", "mp4"))


class TestFailureBeforeTheFirstChunkIsSent:
    """A stage that fails early must fail before anything is yielded.

    The router commits the 200 with the first chunk, so bytes yielded
    ahead of a failure turn an error response into an aborted download.
    """

    def test_failure_after_a_short_output_yields_nothing(self) -> None:
        producer = _python("import sys; sys.stdout.buffer.write(b'header')")
        failing_stage = _stage(
            READ_INPUTS + "sys.stdout.buffer.write(data)\nsys.exit(1)\n"
        )
        stream = _stream_through_ffmpeg(
            [producer], failing_stage, processes=DownloadProcesses()
        )

        with pytest.raises(YouTubeError, match="ffmpeg failed"):
            next(stream)

    def test_output_spanning_several_chunks_still_arrives_whole(self) -> None:
        payload_size = 3 * 65536 + 123
        producer = _python(
            f"import sys; sys.stdout.buffer.write(b'a' * {payload_size})"
        )

        result = b"".join(
            _stream_through_ffmpeg(
                [producer], _passthrough, processes=DownloadProcesses()
            )
        )

        assert result == b"a" * payload_size


@needs_ffmpeg
class TestUnreadableInputOverHttp:
    """What the client sees when the remux cannot read its input (#114)."""

    def test_moov_last_input_is_an_error_response_not_a_video(
        self, client, tmp_path: pathlib.Path
    ) -> None:
        source = _progressive_mp4(tmp_path / "tail.mp4", moov_first=False)

        with patch(
            "app.services.youtube._build_video_commands",
            return_value=(_cat(source), _cat(source)),
        ):
            response = client.get(
                "/api/download",
                params={"url": "https://www.youtube.com/watch?v=test", "fmt": "mp4"},
            )

        assert response.status_code == 500
        assert response.headers["content-type"] == "application/json"
        assert response.json()["error"]["code"] == "download_error"

    def test_moov_first_input_is_served_with_samples(
        self, client, tmp_path: pathlib.Path
    ) -> None:
        source = _progressive_mp4(tmp_path / "head.mp4", moov_first=True)

        with patch(
            "app.services.youtube._build_video_commands",
            return_value=(_cat(source), _cat(source)),
        ):
            response = client.get(
                "/api/download",
                params={"url": "https://www.youtube.com/watch?v=test", "fmt": "mp4"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert "mdat" in _top_level_boxes(response.content, 5)


class TestFinalizeProcessKillsGrandchildren:
    """yt-dlp spawns processes of its own, such as deno and ffmpeg.

    Signalling only the direct child left that grandchild holding two
    HTTPS connections, with no socket timeout of its own, for as long
    as the container lived.
    """

    def test_grandchild_does_not_survive_teardown(self) -> None:
        spawner = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", spawner],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        grandchild_pid = int(process.stdout.readline())
        assert _is_alive(grandchild_pid)

        _finalize_process("fake", process, drainer=None)

        assert _wait_until_dead(grandchild_pid), (
            f"grandchild {grandchild_pid} outlived teardown"
        )
        assert not _is_alive(process.pid)

    def test_teardown_does_not_signal_the_servers_own_group(self) -> None:
        # start_new_session puts each child in its own group, so the
        # killpg target can never be the process running the tests.
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        assert os.getpgid(process.pid) != os.getpgid(0)

        _finalize_process("fake", process, drainer=None)
        assert not _is_alive(process.pid)


class TestGrandchildOutlivingTheParent:
    """The case that matters in production: yt-dlp exits, its child does not.

    yt-dlp starts processes of its own (deno, ffmpeg), so the direct
    child can finish while the grandchild is still running. The stream
    body reaps the child to read its exit status, and a reaped pid can
    be neither found (os.getpgid raises) nor trusted (the number may
    have been reused), so the cleanup has to happen before that.
    """

    def _spawner(self, marker: pathlib.Path, exit_code: int) -> list[str]:
        # The grandchild gets its own stdout: inheriting the pipe would
        # hold it open after the parent exits and the read loop would
        # never see EOF.
        return [
            sys.executable,
            "-c",
            "import subprocess, sys, pathlib\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
            "sys.stdout.buffer.write(b'data')\n"
            "sys.stdout.flush()\n"
            f"sys.exit({exit_code})\n",
        ]

    def _grandchild_pid(self, marker: pathlib.Path) -> int:
        return int(marker.read_text())

    def test_grandchild_is_killed_when_the_parent_exits_cleanly(
        self, tmp_path: pathlib.Path
    ) -> None:
        marker = tmp_path / "grandchild.pid"

        assert (
            b"".join(
                _stream_through_ffmpeg(
                    [self._spawner(marker, 0)],
                    _passthrough,
                    processes=DownloadProcesses(),
                )
            )
            == b"data"
        )

        grandchild = self._grandchild_pid(marker)
        if not _wait_until_dead(grandchild):
            os.kill(grandchild, signal.SIGKILL)
            pytest.fail(f"grandchild {grandchild} outlived a clean exit")

    def test_exit_status_survives_the_group_kill(self, tmp_path: pathlib.Path) -> None:
        # The cleanup signals the group while the child is a zombie.
        # Signals to a zombie are discarded, so the failure must still
        # be reported as the child's own non-zero exit, not as SIGKILL.
        marker = tmp_path / "grandchild.pid"

        with pytest.raises(YouTubeError, match="exited with code 1"):
            list(
                _stream_through_ffmpeg(
                    [self._spawner(marker, 1)],
                    _passthrough,
                    processes=DownloadProcesses(),
                )
            )

        grandchild = self._grandchild_pid(marker)
        if not _wait_until_dead(grandchild):
            os.kill(grandchild, signal.SIGKILL)
            pytest.fail(f"grandchild {grandchild} outlived a failed exit")


class TestCloseWhileTheGeneratorIsSuspended:
    """Issue #105: a client disconnect abandons the response iterator.

    Starlette hands a sync iterator to iterate_in_threadpool, which
    never calls close() on it, so cancelling the response leaves the
    generator suspended at its yield and its finally unreached. Across
    31 measured disconnects the finally ran 13 times, all of them
    garbage-collected tens of seconds late, and never at all against a
    real yt-dlp within 90s -- every disconnect left a yt-dlp and an
    ffmpeg downloading to nowhere.

    DownloadProcesses.close is what the router attaches to the
    response's BackgroundTask, which does run on that path. These tests
    drive it the way the background task does: from outside a generator
    that is never closed.
    """

    def _forever_with_grandchild(self, marker: pathlib.Path) -> list[str]:
        # Streams until killed, so the generator stays suspended at a
        # yield, and starts a grandchild the way yt-dlp starts deno.
        # The grandchild gets its own stdout so it cannot hold the pipe
        # open and mask a failure to kill it.
        return [
            sys.executable,
            "-c",
            "import os, subprocess, sys, pathlib, time\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"pathlib.Path({str(marker)!r}).write_text(\n"
            "    str(os.getpid()) + ' ' + str(child.pid))\n"
            "while True:\n"
            "    sys.stdout.buffer.write(b'x' * 4096)\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(0.01)\n",
        ]

    def test_close_kills_the_pipeline_the_generator_never_released(
        self, tmp_path: pathlib.Path
    ) -> None:
        marker = tmp_path / "pids"
        processes = DownloadProcesses()
        stream = _stream_through_ffmpeg(
            [self._forever_with_grandchild(marker)], _passthrough, processes=processes
        )

        assert next(stream)  # the pipeline is live and streaming
        child, grandchild = (int(pid) for pid in marker.read_text().split())
        assert _is_alive(child)
        assert _is_alive(grandchild)

        # The disconnect: the generator is abandoned mid-stream, still
        # suspended at its yield, and close() is all that runs.
        processes.close()

        try:
            assert _wait_until_dead(child), f"yt-dlp stand-in {child} survived"
            assert _wait_until_dead(grandchild), (
                f"ffmpeg stand-in {grandchild} survived"
            )
        finally:
            for pid in (child, grandchild):
                if _is_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            stream.close()

    def test_generator_teardown_after_close_is_a_no_op(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The other half of the race: the background task closes first,
        # then the abandoned generator is finally collected and its
        # finally calls close() again. By then the pid has been reaped
        # and may belong to someone else, so nothing may be signalled.
        marker = tmp_path / "pids"
        processes = DownloadProcesses()
        stream = _stream_through_ffmpeg(
            [self._forever_with_grandchild(marker)], _passthrough, processes=processes
        )

        assert next(stream)
        processes.close()

        with patch("app.services.youtube.os.killpg") as killpg:
            stream.close()

        killpg.assert_not_called()


class TestDrainerThatCannotStart:
    """The stderr drainer is started after the process it drains.

    threading.Thread.start() raises RuntimeError when the interpreter
    cannot create a thread, so between Popen and a running drainer there
    is a window where the subprocess exists but nothing owns it. A
    version of this that recorded the process only after the drainer had
    started leaked it: the generator's finally called close(), which
    found nothing registered, and the subprocess ran on.
    """

    def test_process_is_killed_when_the_drainer_cannot_start(self) -> None:
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        started: list[int] = []

        def explode(name: str, process: subprocess.Popen[bytes], tail: object) -> None:
            started.append(process.pid)
            raise RuntimeError("can't start new thread")

        stream = _stream_through_ffmpeg(
            [cmd], _passthrough, processes=DownloadProcesses()
        )

        with (
            patch("app.services.youtube._start_stderr_drainer", explode),
            pytest.raises(RuntimeError),
        ):
            next(stream)

        assert started, "the drainer was never reached"
        pid = started[0]
        try:
            assert _wait_until_dead(pid), f"subprocess {pid} was left running"
        finally:
            if _is_alive(pid):
                os.kill(pid, signal.SIGKILL)
