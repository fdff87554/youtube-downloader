"""Shared test fixtures.

The second half is the yt-dlp conformance adapter: the single seam
``tests/test_ytdlp_conformance.py`` reaches this repository through.
That test imports no repository code and is byte-identical in all
three projects that share it, so everything project-specific lives
here. See ``docs/ytdlp-conformance/README.md``.
"""

import pytest
from fastapi.testclient import TestClient

from app.limiter import limiter
from app.main import create_app

# The conformance adapter below reaches this repository through the
# entry points the production code calls, not the generic builder
# underneath them: a builder invoked directly from here cannot notice
# one entry point being changed on its own.
from app.services.youtube import (
    InvalidURLError,
    _base_opts,
    _build_audio_command,
    _build_info_command,
    _build_video_commands,
    _neutralize_control_chars,
    normalize_youtube_url,
)
from tests.test_ytdlp_conformance import DOWNLOAD, EXTRACT, CallSite


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> None:
    """Clear the shared in-memory rate limiter before every test.

    ``limiter`` is a module-level singleton, so per-IP counts survive
    across tests and across TestClient instances. Without this, a test
    that drives a limit to exhaustion makes every later test in the
    session see 429 instead of the status code it asserts -- which made
    the suite depend on declaration order.
    """
    limiter.reset()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a test client for the FastAPI application.

    Sets DEBUG=true so the CORS configuration accepts a missing
    ALLOWED_ORIGINS without raising during create_app(). The rate
    limiter is reset by the autouse ``reset_rate_limiter`` fixture.
    """
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    app = create_app()
    return TestClient(app)


# --------------------------------------------------------------------
# yt-dlp conformance adapter
# --------------------------------------------------------------------
_SAMPLE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Every quality the API accepts. _resolve_video_format builds a
# different selector per tier, so checking one of them checks one of
# them.
_QUALITIES = ("best", "1080", "720", "480")


class YtDlpSubject:
    """What the conformance test needs to know about this repository."""

    def call_sites(self) -> list[CallSite]:
        """Every place this repository hands work to yt-dlp.

        List them all, not a representative one, and reach them
        through the functions the production code calls. The checks
        that run per site are the ones a single forgotten call site
        silently defeats: an extractor pin applied in three places out
        of four is the shape of the failure this is here to catch.

        Note what this repository declares and what it does not. It
        drives yt-dlp in-process only to read metadata, and every byte
        it downloads goes through a subprocess, so no site here is both
        in-process and a download. Saying so is the point of
        ``purpose``.
        """
        sites = [
            CallSite(
                name="extract_video_info",
                purpose=EXTRACT,
                options=_base_opts() | {"noplaylist": True},
            ),
            CallSite(
                name="extract_playlist_info",
                purpose=EXTRACT,
                options=_base_opts()
                | {"extract_flat": "in_playlist", "noplaylist": False},
            ),
            CallSite(
                name="_build_info_command",
                purpose=EXTRACT,
                arguments=_build_info_command(_SAMPLE_URL),
            ),
            CallSite(
                name="_build_audio_command (mp3)",
                purpose=DOWNLOAD,
                arguments=_build_audio_command(_SAMPLE_URL),
            ),
        ]
        for quality in _QUALITIES:
            video, audio = _build_video_commands(quality)
            sites.append(
                CallSite(
                    name=f"_build_video_commands({quality}) video",
                    purpose=DOWNLOAD,
                    arguments=video,
                )
            )
            sites.append(
                CallSite(
                    name=f"_build_video_commands({quality}) audio track",
                    purpose=DOWNLOAD,
                    arguments=audio,
                )
            )
        return sites

    def canonicalize(self, url: str) -> str | None:
        """The rebuilt URL, or ``None`` when this repository refuses it.

        ``None`` must mean refused: a function that raises is wrapped
        here, because the test reads ``None`` as the declared rejection
        and an exception as a bug.

        Returning a URL is not a claim that the URL is safe. This
        repository takes the URL as the user typed it, so the rebuild
        only lowercases scheme and host and lets "/about/" through; the
        extractor pin is what refuses that one. The test checks the
        pair, not either layer alone.
        """
        try:
            return normalize_youtube_url(url)
        except InvalidURLError:
            return None

    def neutralize(self, text: str) -> str:
        """Remote-controlled text, made safe to display or log."""
        return _neutralize_control_chars(text)


@pytest.fixture(scope="session")
def ytdlp_subject() -> YtDlpSubject:
    return YtDlpSubject()


# The gaps this repository has not closed yet, by node id. They are
# marked from here rather than in the test file because the test file
# is byte-identical across the three projects and its canonical region
# is hashed: an xfail written into it would be drift. Every entry cites
# the issue that closes it, and check-conformance-drift.sh fails when
# more items are marked than xfail_budget allows, so this list can only
# shrink.
CONFORMANCE_XFAILS = {
    "tests/test_ytdlp_conformance.py::TestTheByteCeilingIsDeclaredAndReachable"
    "::test_every_downloading_site_rejects_unmeterable_formats": (
        "C7.selectable: no download selector constrains protocol, so an "
        "HLS rendition is selectable; see #138"
    ),
    "tests/test_ytdlp_conformance.py::TestTheByteCeilingIsDeclaredAndReachable"
    "::test_no_downloading_selector_falls_back_to_an_unmeterable_format": (
        "C7.selectable: bestaudio/best and the best tier fall back to an "
        "unconstrained branch; see #138"
    ),
}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark the known conformance gaps, and fail if one goes stale.

    A node id that no longer matches anything is a stale entry: the
    test it excused was renamed or deleted, and leaving it behind
    means the budget reserves room for a gap nobody can close.

    Only entries whose module was collected are judged, or every
    targeted run -- ``pytest tests/test_health.py`` -- would fail for
    not having collected the conformance module.
    """
    collected_modules = {item.nodeid.split("::", 1)[0] for item in items}
    unmatched = {
        nodeid
        for nodeid in CONFORMANCE_XFAILS
        if nodeid.split("::", 1)[0] in collected_modules
    }
    for item in items:
        reason = CONFORMANCE_XFAILS.get(item.nodeid)
        if reason is None:
            continue
        unmatched.discard(item.nodeid)
        item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
    if unmatched:
        raise pytest.UsageError(
            "conformance xfail entries match no collected test: "
            + ", ".join(sorted(unmatched))
        )
