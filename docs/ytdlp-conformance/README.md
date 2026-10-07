# Applying the yt-dlp conformance test

The invariants are specified in
[`../ytdlp-invariants.md`](../ytdlp-invariants.md). This directory holds
what a repository copies in order to have them checked mechanically.
Three repositories use it: this one, Whisper-UI and voice-forge.

The test runs offline, touches no network, and needs no fixtures beyond
the adapter described below.

## What to copy where

- `test_ytdlp_conformance.py.example` goes to the repository's test
  path, and is **not** edited.
- `conftest-fixture.py.example` is appended to `conftest.py` and filled
  in.
- `pyproject-profile.toml.example` is appended to `pyproject.toml` and
  filled in.
- `check-conformance-drift.sh` is copied as-is and run from the
  repository root in CI.

`report-sibling-profiles.sh` is **not** copied. It runs only in this
repository, which is the canonical home; see
[Drift control](#drift-control) below.

Drop the `.example` suffix on the first one. Keep its name
`test_ytdlp_conformance.py`, because the drift check looks for it.

The test holds **no import of repository code**. The adapter in
`conftest-fixture.py.example` is the single seam, which is what lets
the test itself stay byte-identical in all three repositories while the
code around it differs completely.

## Required: exclude the copied test from the formatter

Add the copied path to ruff's format exclusions, and leave it in
`ruff check`'s scope:

```toml
[tool.ruff.format]
exclude = ["tests/test_ytdlp_conformance.py"]
```

This is not a style preference, it is what makes the drift check
possible. The three repositories do not share a line length --
Whisper-UI uses 120, this repository and voice-forge use 88 -- so each
one's `ruff format` rewrites the shared region differently. Measured on
the template as shipped: at 120, `ruff format` joins the call arguments
in `_extractors_matching` and collapses the implicit string
concatenation in `_load_profile`, and the region's hash stops matching
in that repository and only there.

`ruff check` is unaffected by the exclusion. The template is clean
under the union of all three repositories' selected rules, verified
against the ruff that the pre-commit hook pins.

## The canonical region

```python
# --- ytdlp-conformance:canonical-begin ---
...
# --- ytdlp-conformance:canonical-end ---
```

Everything between the sentinels is shared and must stay
byte-identical. `check-conformance-drift.sh` hashes exactly that span
and compares it to `canonical_region_sha256` in the profile.

The region is hashed instead of the whole file because **additions
below the end sentinel are expected and legitimate**. That is the
lesson from the copied `.claude/rules/` files: of nine copies across
these three repositories only two were byte-identical to upstream, and
at least one of the differences was a valid downstream addition. A
whole-file hash cannot tell that apart from rot, and a check that cries
wolf teaches people to ignore it.

So: repository-specific tests go **below** the end sentinel, in the
same file, and nothing above it is edited locally. A change the region
genuinely needs goes upstream first, into this directory, and then the
copies and their recorded hashes follow.

## Waivers, and what counts as covered

Each test class carries an `INVARIANT` tag, and one autouse fixture
turns a waiver in the profile into a skip for that class, with the
waiver's own text as the skip reason. A waiver is therefore visible in
the test report, not only in the manifest.

`COVERED_INVARIANTS` in the test lists what the file checks
mechanically, and `TestEveryInvariantIsAccountedFor` fails when an
invariant has neither a test nor a waiver. That is what stops "no
test" from reading the same as "passing". **C5 is in that position
today** -- error-message content is not visible through the adapter --
so every repository has to waive C5 deliberately, with an issue link.

## Filling in the profile

Every key in `pyproject-profile.toml.example` is mandatory and none has
a default. That is deliberate: a key added upstream turns all three
repositories red at once, rather than passing silently in the two that
did not notice.

Most values are **expected to differ** per repository, and differing is
not drift. The extractor pin is the clearest case: `["youtube.*"]` here
because this project accepts the URL as the user typed it, `["youtube"]`
in the other two because they rebuild it first. The test checks that
each declaration matches reality, not that the three declarations match
each other.

Two keys need a word of warning:

- `accepts_urls` / `rejects_urls` are checked against the **real**
  extractor registry. A URL in `rejects_urls` must reach no extractor,
  either because the rebuild refuses it or because the pin matches
  nothing for it. Both layers count; neither has to do it alone.
- `markers_verified_against` fails the test as soon as the installed
  yt-dlp moves. That is the intent -- the coupling gets re-verified
  deliberately. Do not bump the string to clear the red; re-check the
  markers and then bump it.

## Drift control

`check-conformance-drift.sh` is the part that runs in each repository.
It checks two things, both offline:

- the canonical region still hashes to `canonical_region_sha256`;
- the number of `pytest.mark.xfail` markers is within `xfail_budget`.

Add it as a CI step next to the test run:

```bash
bash check-conformance-drift.sh tests/test_ytdlp_conformance.py pyproject.toml
```

Both arguments are optional and default to those values.

The third layer runs only here: `report-sibling-profiles.sh` reads each
public sibling's declared `spec_version` and `canonical_region_sha256`
through the GitHub API and prints them into the job summary. It is
report-only and the job sets `continue-on-error`, because a pull request
in one repository should not be blocked by another's state. A repository
that has not adopted the spec reports as `not declared` rather than
failing the step -- a check that goes red for a reason nobody can act on
is a check people learn to ignore.

It covers this repository and Whisper-UI, and not voice-forge, which is
private: reading it from a public repository's CI would mean holding a
credential with read access to a private repository. voice-forge runs
the first two layers in its own CI, which are the stronger two. The gap
is written down in the spec so that it stays countable.

## What this test does not cover

- It never reaches the network, so it says nothing about whether
  extraction currently works against YouTube. That is the job of the
  runtime self-check, the third layer in the spec.
- C5 (no internal detail in error messages) and the parts of C7 that
  depend on observing a real transfer are not checkable from the
  adapter surface. A repository that has not yet wired them records an
  `xfail` with an issue link, and `xfail_budget` keeps that list from
  growing.
- It reaches two of yt-dlp's private attributes, `_ies` and
  `_js_runtimes`. Both are guarded with `hasattr` and **fail** rather
  than skip when they are missing, because a silent skip is the failure
  mode the whole mechanism exists to prevent. A yt-dlp release that
  removes them turns the test red on purpose; it needs rewriting
  against whatever replaced them, not relaxing.

## Removing it again

Delete the copied test, the adapter from `conftest.py`, the
`[tool.ytdlp_conformance]` table, the `[tool.ruff.format]` exclusion and
the CI step. Nothing else depends on any of it; there is no shared
package to uninstall, which is the point of the copy-based approach and
also its cost. The cost is written down in the spec's
[exit criteria](../ytdlp-invariants.md#exit-criteria), together with the
conditions under which a shared package becomes the better answer.
