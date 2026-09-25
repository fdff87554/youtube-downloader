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
    _finalize_process,
    _run_piped_process,
    _stream_through_ffmpeg,
)

# Linux-only: the project ships as a Linux container and CI runs on
# ubuntu-latest.
PROC_STATUS = "/proc/{pid}/stat"


def _is_alive(pid: int) -> bool:
    """Whether pid exists and is not a zombie awaiting reaping."""
    try:
        with open(PROC_STATUS.format(pid=pid)) as stat:
            state = stat.read().rsplit(") ", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


# Stands in for our ffmpeg stage: copies stdin to stdout as it arrives,
# so a stream that never ends still reaches the reader.
PASSTHROUGH = [
    sys.executable,
    "-c",
    "import sys\n"
    "while chunk := sys.stdin.buffer.read1(65536):\n"
    "    sys.stdout.buffer.write(chunk)\n"
    "    sys.stdout.buffer.flush()\n",
]


def _python(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def _wait_until_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestRunPipedProcess:
    def test_yields_full_stdout_as_chunks(self) -> None:
        # Payload spans multiple CHUNK_SIZE (64 KiB) reads to verify
        # chunk assembly is lossless.
        payload_size = 200_000
        cmd = [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.buffer.write(b'a' * {payload_size})",
        ]

        result = b"".join(
            _run_piped_process(cmd, name="fake", processes=DownloadProcesses())
        )

        assert result == b"a" * payload_size

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
            _run_piped_process(cmd, name="fake", processes=DownloadProcesses())
        )

        assert result == b"payload"

    def test_raises_youtube_error_when_binary_missing(self) -> None:
        with pytest.raises(YouTubeError, match="not installed"):
            list(
                _run_piped_process(
                    ["/no/such/binary"], name="missing", processes=DownloadProcesses()
                )
            )

    def test_raises_when_process_exits_non_zero_after_empty_stdout(self) -> None:
        # The failure mode that made an unavailable video look like a
        # successful download: no stdout, non-zero exit, generator ends.
        cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]

        with pytest.raises(YouTubeError, match="exited with code 1"):
            list(_run_piped_process(cmd, name="fake", processes=DownloadProcesses()))

    def test_raises_when_process_exits_non_zero_after_partial_stdout(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            "import sys\nsys.stdout.buffer.write(b'partial')\nsys.exit(1)\n",
        ]

        with pytest.raises(YouTubeError):
            list(_run_piped_process(cmd, name="fake", processes=DownloadProcesses()))

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
            list(_run_piped_process(cmd, name="fake", processes=DownloadProcesses()))

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
            list(_run_piped_process(cmd, name="fake", processes=DownloadProcesses()))

    def test_clean_exit_with_output_does_not_raise(self) -> None:
        cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok')"]

        assert (
            b"".join(
                _run_piped_process(cmd, name="fake", processes=DownloadProcesses())
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
            _stream_through_ffmpeg(producer, PASSTHROUGH, processes=DownloadProcesses())
        )

        assert result == b"a" * payload_size

    def test_raises_when_a_binary_is_missing(self) -> None:
        with pytest.raises(YouTubeError, match="not installed"):
            list(
                _stream_through_ffmpeg(
                    ["/no/such/binary"], PASSTHROUGH, processes=DownloadProcesses()
                )
            )

    def test_reports_the_ffmpeg_stage_failing(self) -> None:
        producer = _python("import sys; sys.stdout.buffer.write(b'data')")
        failing_stage = _python(
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "sys.stderr.write('Invalid data found when processing input\\n')\n"
            "sys.exit(1)\n"
        )

        with pytest.raises(YouTubeError, match="ffmpeg failed: Invalid data"):
            list(
                _stream_through_ffmpeg(
                    producer, failing_stage, processes=DownloadProcesses()
                )
            )

    def test_reports_yt_dlp_when_both_stages_fail(self) -> None:
        # ffmpeg failing on a truncated input is only a consequence;
        # the reason worth reporting is on yt-dlp's stderr.
        producer = _python(
            "import sys\nsys.stderr.write('ERROR: Video unavailable\\n')\nsys.exit(1)\n"
        )
        failing_stage = _python("import sys; sys.stdin.buffer.read(); sys.exit(1)")

        with pytest.raises(VideoNotFoundError, match="Video unavailable"):
            list(
                _stream_through_ffmpeg(
                    producer, failing_stage, processes=DownloadProcesses()
                )
            )


class TestFinalizeProcessKillsGrandchildren:
    """yt-dlp spawns ffmpeg itself when it has to mux two streams.

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
    """The case that matters in production: yt-dlp exits, ffmpeg does not.

    yt-dlp starts ffmpeg itself to mux separate streams, so the direct
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
                _run_piped_process(
                    self._spawner(marker, 0), name="fake", processes=DownloadProcesses()
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
                _run_piped_process(
                    self._spawner(marker, 1), name="fake", processes=DownloadProcesses()
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
        # yield, and starts a grandchild the way yt-dlp starts ffmpeg.
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
        stream = _run_piped_process(
            self._forever_with_grandchild(marker), name="fake", processes=processes
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
        stream = _run_piped_process(
            self._forever_with_grandchild(marker), name="fake", processes=processes
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

        stream = _run_piped_process(cmd, name="fake", processes=DownloadProcesses())

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
