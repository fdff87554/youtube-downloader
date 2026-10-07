"""The claims ``conformance_xfail_plugin`` makes about its own counting.

The plugin is what stands between the ``xfail`` backlog and silent
growth, and two successive versions of it returned zero while pytest
saw markers: one parsed pytest's output and depended on how the path
was spelled, one counted from ``pytest_collection_modifyitems`` and
depended on hook ordering. Both passed every manual check that was run
at the time. These run on every CI build instead.

Each case drives a real pytest through ``pytester`` rather than
calling the hook directly, because the failures were about *when*
pytest calls it, which a direct call cannot reproduce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

PLUGIN_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "ytdlp-conformance"
    / "conformance_xfail_plugin.py"
)

PLUGIN_NAME = "conformance_xfail_plugin"
COUNT_FILE_ENV = "CONFORMANCE_XFAIL_COUNT_FILE"

THREE_PLAIN_TESTS = """
def test_one():
    assert True


def test_two():
    assert True


def test_three():
    assert True
"""

MARKING_CONFTEST = """
import pytest


{decorator}def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.xfail(reason="added by the conftest"))
"""


def _run(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    *arguments: str,
) -> list[str]:
    """Collect through the plugin and return the lines it wrote.

    A subprocess, not ``runpytest_inprocess``: the plugin writes from a
    collection hook, and running in-process would mix its hooks with
    the outer run's.
    """
    (pytester.path / f"{PLUGIN_NAME}.py").write_text(
        PLUGIN_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    count_file = pytester.path / "count.txt"
    monkeypatch.setenv(COUNT_FILE_ENV, str(count_file))
    pytester.runpytest_subprocess(
        *arguments, "--collect-only", "-p", PLUGIN_NAME, "-p", "no:cacheprovider"
    )
    assert count_file.exists(), "the plugin wrote no count file"
    return count_file.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "decorator",
    [
        pytest.param("", id="plain"),
        pytest.param("@pytest.hookimpl(tryfirst=True)\n", id="tryfirst"),
        pytest.param("@pytest.hookimpl(trylast=True)\n", id="trylast"),
    ],
)
def test_markers_added_by_a_conftest_are_counted_whenever_it_runs(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, decorator: str
) -> None:
    # The whole point of counting from pytest_collection_finish. A
    # plain conftest hook happens to run before a -p plugin's, so the
    # earlier version agreed on two of these three and reported 0 for
    # trylast while pytest reported 3 xpassed.
    pytester.makepyfile(test_mod=THREE_PLAIN_TESTS)
    pytester.makeconftest(MARKING_CONFTEST.format(decorator=decorator))
    assert _run(pytester, monkeypatch, "test_mod.py")[0] == "3"


def test_a_module_level_pytestmark_behind_an_alias_is_counted(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two lines turn a whole module into xfail while a grep for
    # "pytest.mark.xfail" finds nothing, which is what sank the
    # text-scanning version.
    pytester.makepyfile(
        test_mod="from pytest import mark\n\n"
        'pytestmark = mark.xfail(strict=False, reason="all of it")\n'
        + THREE_PLAIN_TESTS
    )
    assert _run(pytester, monkeypatch, "test_mod.py")[0] == "3"


def test_an_explicit_decorator_counts_only_the_test_it_marks(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        test_mod="import pytest\n\n\n"
        '@pytest.mark.xfail(reason="just this one")\n'
        "def test_marked():\n    assert True\n\n\n"
        "def test_unmarked():\n    assert True\n"
    )
    written = _run(pytester, monkeypatch, "test_mod.py")
    assert written[0] == "1"
    assert written[1].endswith("::test_marked")


def test_an_unmarked_module_counts_zero(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other direction: a plugin that always reported a number
    # would pass every case above.
    pytester.makepyfile(test_mod=THREE_PLAIN_TESTS)
    assert _run(pytester, monkeypatch, "test_mod.py") == ["0"]


@pytest.mark.parametrize("spelling", ["bare", "dot-slash", "absolute"])
def test_the_count_does_not_depend_on_how_the_path_is_spelled(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    # The version that parsed pytest's listing anchored on the path it
    # had been given, so the two spellings below reported 0 and exited
    # 0 on a module full of xfails.
    pytester.makepyfile(test_mod=THREE_PLAIN_TESTS)
    pytester.makeconftest(MARKING_CONFTEST.format(decorator=""))
    argument = {
        "bare": "test_mod.py",
        "dot-slash": "./test_mod.py",
        "absolute": str(pytester.path / "test_mod.py"),
    }[spelling]
    assert _run(pytester, monkeypatch, argument)[0] == "3"


def test_the_count_does_not_depend_on_pytest_verbosity(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(test_mod=THREE_PLAIN_TESTS)
    pytester.makeconftest(MARKING_CONFTEST.format(decorator=""))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    assert _run(pytester, monkeypatch, "test_mod.py")[0] == "3"


def test_nothing_is_written_without_the_destination_variable(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The drift script treats a missing count file as a failure rather
    # than a count of zero, so the plugin must not invent one.
    pytester.makepyfile(test_mod=THREE_PLAIN_TESTS)
    (pytester.path / f"{PLUGIN_NAME}.py").write_text(
        PLUGIN_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.delenv(COUNT_FILE_ENV, raising=False)
    pytester.runpytest_subprocess(
        "test_mod.py", "--collect-only", "-p", PLUGIN_NAME, "-p", "no:cacheprovider"
    )
    assert not list(pytester.path.glob("count.txt"))
