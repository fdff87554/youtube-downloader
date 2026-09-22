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

import pytest

from app.services.youtube import (
    UnsupportedURLError,
    VideoNotFoundError,
    YouTubeError,
    _finalize_process,
    _run_piped_process,
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

        result = b"".join(_run_piped_process(cmd, name="fake"))

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

        result = b"".join(_run_piped_process(cmd, name="fake"))

        assert result == b"payload"

    def test_raises_youtube_error_when_binary_missing(self) -> None:
        with pytest.raises(YouTubeError, match="not installed"):
            list(_run_piped_process(["/no/such/binary"], name="missing"))

    def test_raises_when_process_exits_non_zero_after_empty_stdout(self) -> None:
        # The failure mode that made an unavailable video look like a
        # successful download: no stdout, non-zero exit, generator ends.
        cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]

        with pytest.raises(YouTubeError, match="exited with code 1"):
            list(_run_piped_process(cmd, name="fake"))

    def test_raises_when_process_exits_non_zero_after_partial_stdout(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            "import sys\nsys.stdout.buffer.write(b'partial')\nsys.exit(1)\n",
        ]

        with pytest.raises(YouTubeError):
            list(_run_piped_process(cmd, name="fake"))

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
            list(_run_piped_process(cmd, name="fake"))

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
            list(_run_piped_process(cmd, name="fake"))

    def test_clean_exit_with_output_does_not_raise(self) -> None:
        cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok')"]

        assert b"".join(_run_piped_process(cmd, name="fake")) == b"ok"


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
            b"".join(_run_piped_process(self._spawner(marker, 0), name="fake"))
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
            list(_run_piped_process(self._spawner(marker, 1), name="fake"))

        grandchild = self._grandchild_pid(marker)
        if not _wait_until_dead(grandchild):
            os.kill(grandchild, signal.SIGKILL)
            pytest.fail(f"grandchild {grandchild} outlived a failed exit")
