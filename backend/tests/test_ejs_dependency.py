"""The challenge solver scripts have to be installed, and match yt-dlp.

yt-dlp solves YouTube's JavaScript challenges with external scripts
(EJS). The image ships a Deno runtime for them, but a runtime with no
script to run is only half of it: with the scripts missing, every
extraction reports "Signature solving failed" and "n challenge solving
failed", and formats that need signature deciphering disappear.

Nothing else in the suite can catch that. The unit tests mock yt-dlp
entirely, and the failure is invisible at runtime because both code
paths pass --quiet/--no-warnings, so a lock regenerated without the
dependency would degrade downloads silently.
"""

from __future__ import annotations

import importlib.metadata as metadata
import re

import pytest

EJS_PACKAGE = "yt-dlp-ejs"
# yt-dlp declares the version it expects in its [pin] extra. We depend on
# the package directly instead of through that extra, which also pulls in
# eight packages this service does not use, so the coupling is checked
# here rather than by the resolver.
PINNED_EJS_REQUIREMENT = re.compile(
    r"^yt-dlp-ejs\s*==\s*(?P<version>[\w.]+)\s*;.*extra\s*==\s*['\"]pin['\"]"
)


def _expected_ejs_version() -> str:
    """The yt-dlp-ejs version the installed yt-dlp pins in its extras."""
    requirements = metadata.metadata("yt-dlp").get_all("Requires-Dist") or []
    for requirement in requirements:
        match = PINNED_EJS_REQUIREMENT.match(requirement)
        if match:
            return match.group("version")
    pytest.fail(
        "installed yt-dlp declares no pinned yt-dlp-ejs version; its extras "
        "may have been restructured, so this check needs revisiting"
    )


class TestEjsIsInstalled:
    def test_ejs_package_is_installed(self) -> None:
        # metadata() raises PackageNotFoundError when it is absent, which
        # is the failure worth reporting -- not an import error later.
        assert metadata.version(EJS_PACKAGE)

    def test_ejs_matches_yt_dlp_requirement(self) -> None:
        # Regenerating the lock after a yt-dlp bump can leave the scripts
        # behind, and a mismatched pair fails at runtime rather than here
        # unless something checks it.
        assert metadata.version(EJS_PACKAGE) == _expected_ejs_version()
