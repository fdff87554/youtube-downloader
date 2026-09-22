"""The debug docs page must be loadable under the CSP nginx serves it with.

Asserting HTTP 200 is not enough: Swagger UI pulls its bundle from a
CDN and boots from an inline script, so a policy that forbids either
leaves a page that answers 200 with a blank body. These tests compare
the hosts the page actually references against the policy declared for
that path in docker/nginx.conf, which catches both a tightened policy
and FastAPI switching CDNs.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

NGINX_CONF = Path(__file__).resolve().parents[2] / "docker" / "nginx.conf"
DOCS_PATH = "/api/docs"


def _docs_html(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com")
    response = TestClient(create_app()).get(DOCS_PATH)
    assert response.status_code == 200
    return response.text


def _docs_policy() -> dict[str, set[str]]:
    """Parse the /api/docs entry of the CSP map into directive -> sources."""
    conf = NGINX_CONF.read_text()
    match = re.search(rf'^\s*{re.escape(DOCS_PATH)}\s+"([^"]+)";', conf, re.MULTILINE)
    assert match, f"no CSP map entry for {DOCS_PATH} in {NGINX_CONF}"
    directives = {}
    for part in match.group(1).split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = set(tokens[1:])
    return directives


def _referenced_hosts(html: str, tag_attr: str) -> set[str]:
    pattern = rf'<{tag_attr}[^>]*?(?:src|href)="(https://[^"]+)"'
    return {
        re.sub(r"^https://", "", url).split("/")[0] for url in re.findall(pattern, html)
    }


class TestDebugDocsCsp:
    def test_script_sources_are_permitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        html = _docs_html(monkeypatch)
        policy = _docs_policy()

        for host in _referenced_hosts(html, "script"):
            assert f"https://{host}" in policy["script-src"], host

    def test_inline_script_is_permitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = _docs_html(monkeypatch)

        # FastAPI's template initialises SwaggerUIBundle inline.
        assert "<script>" in html
        assert "'unsafe-inline'" in _docs_policy()["script-src"]

    def test_stylesheet_sources_are_permitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        html = _docs_html(monkeypatch)
        policy = _docs_policy()

        for host in _referenced_hosts(html, "link"):
            allowed = policy["style-src"] | policy["img-src"]
            assert f"https://{host}" in allowed, host

    def test_other_paths_keep_the_strict_policy(self) -> None:
        # The loosened policy must not leak past the docs page.
        conf = NGINX_CONF.read_text()
        default = re.search(r'^\s*default\s+"([^"]+)";', conf, re.MULTILINE)
        assert default, "no default CSP map entry"
        assert "cdn.jsdelivr.net" not in default.group(1)
        assert "script-src 'self';" in default.group(1)
