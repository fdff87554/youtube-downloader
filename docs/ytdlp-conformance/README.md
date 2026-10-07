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

The accounting unit is an **aspect**, not a whole invariant. C7 asks
for a ceiling that holds on every downloader path and C8 for a bound on
the whole download; neither is one check, and a test covering one part
must not clear the invariant. The spec's
[Aspects](../ytdlp-invariants.md#aspects) section is the list.

In the test, `SPEC_ASPECTS` holds all of them, `COVERED_ASPECTS` the
ones this file checks, and `ASPECT_OF_TEST` maps each test to the
aspect it stands for. One autouse fixture reads that map and turns a
waiver into a skip carrying the waiver's own text, so a waiver shows up
in the test report and not only in the manifest.

Three tests guard the accounting itself:

- an aspect with neither a test nor a waiver fails;
- an aspect listed in `COVERED_ASPECTS` with no test behind it fails,
  so adding an entry there cannot excuse a waiver;
- a test naming an aspect the spec does not define fails.

**Three aspects have to be waived by every repository**, because the
shared test cannot check them: `C5` (error-message content is not
visible through the adapter), `C7.landed` (a repository that streams
its media lands no file, and one that does exposes no hook for a shared
test), and `C8.deadline` (none of the three has a wall-clock bound, and
a shared test cannot invent the mechanism it would check).

A fourth, **`C7.external`, is required of any repository that downloads
through a subprocess**, because a subprocess takes its options as argv
and reports nothing back: the shared test can neither watch the
transfer nor measure what landed. What covers that aspect is a test
that demands the waiver. Checking the argv for `--max-filesize`
instead was considered and rejected -- it is consulted once,
pre-transfer, only with a known `Content-Length`, so asserting its
presence would record conformance that is not there.

Each waiver needs an issue link.

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
  extractor registry, `generic` included. A URL in `rejects_urls` must
  reach no extractor, either because the rebuild refuses it or because
  the pin matches nothing for it. Both layers count; neither has to do
  it alone. The pin itself must not leave `generic` enabled, which is
  checked separately -- with it enabled, the malicious-authority URL
  and the link-local metadata address both match it.
- `max_bytes` has no "unlimited" value. A repository that has not
  decided on a ceiling waives `C7.metered` and says why, rather than
  recording a number nothing enforces.
- `markers_verified_against` fails the test as soon as the installed
  yt-dlp moves. That is the intent -- the coupling gets re-verified
  deliberately. Do not bump the string to clear the red; re-check the
  markers and then bump it.

## Drift control

`check-conformance-drift.sh` is the part that runs in each repository.
It checks two things, both offline:

- the canonical region still hashes to `canonical_region_sha256`;
- no more collected items carry an `xfail` marker than `xfail_budget`
  allows, counted by asking pytest:
  `pytest <test> -m xfail --collect-only -q`.

Add it as a CI step next to the test run, with the project's test
environment active:

```bash
bash check-conformance-drift.sh tests/test_ytdlp_conformance.py pyproject.toml
```

Both arguments are optional and default to those values.

**The budget is counted by pytest, and from outside the test module, on
purpose.** A text scan cannot see what pytest sees, and the gap is
exploitable with ordinary pytest: two lines outside the hashed region --

```python
from pytest import mark

pytestmark = mark.xfail(strict=False, reason="...")
```

-- turn every test in the module into an `xfail`. An earlier version
grepped for `pytest.mark.xfail`, reported `0/0 xfail` and exited 0 while
pytest reported `3 skipped, 4 xfailed, 21 xpassed`. Putting the count in
a test inside the conformance module does not fix it either: those same
two lines mark that test too, so its failure is reported as an `xfail`
and the run stays green. A check cannot police the module it lives in.

Verified against the three routes a marker can arrive by -- an explicit
decorator, an aliased module-level `pytestmark`, and
`pytest_collection_modifyitems` in a `conftest.py` -- plus a
non-integer budget, which an earlier version reported as a shell error
and then exited 0 on.

The third layer runs only here: `report-sibling-profiles.sh` reads each
public sibling's declared `spec_version` and `canonical_region_sha256`
through the GitHub API and prints them into the job summary. It is
report-only and the job sets `continue-on-error`, because a pull request
in one repository should not be blocked by another's state. A repository
that has not adopted the spec reports as `not declared` rather than
failing the step -- a check that goes red for a reason nobody can act on
is a check people learn to ignore.

Report-only is not the same as silent. Each row says which of three
things happened, and a failed query carries the error with it:

| Row                  | Meaning                                             |
| -------------------- | --------------------------------------------------- |
| a version and a hash | the profile was read                                |
| `not declared`       | the repository has no `[tool.ytdlp_conformance]`    |
| `no manifest found`  | the API returned 404 for every candidate path       |
| `unavailable`        | the query failed, with the error in the next column |

Collapsing those is how a broken token comes to look like a sibling
that has not adopted the spec -- which is what the first version did,
reporting `no manifest found` for an HTTP 401.

It covers this repository and Whisper-UI, and not voice-forge, which is
private: reading it from a public repository's CI would mean holding a
credential with read access to a private repository. voice-forge runs
the first two layers in its own CI, which are the stronger two. The gap
is written down in the spec so that it stays countable.

## What this test does not cover

- It never reaches the network, so it says nothing about whether
  extraction currently works against YouTube. That is the job of the
  runtime self-check, the third layer in the spec.
- The three aspects listed above are not checkable from the adapter
  surface and are waived rather than tested. A repository with a gap it
  intends to close instead records an `xfail` with an issue link, and
  `xfail_budget` keeps that list from growing.
- `C7.selectable` is checked by **evaluating** each download selector
  against two offline format pools, not by searching it for a protocol
  filter. One pool offers meterable and unmeterable renditions, with
  the unmeterable ones carrying the higher bitrate -- protocol is only
  a tiebreak in yt-dlp's sort order, so an equal-quality pool makes an
  unconstrained selector look safe. The other offers nothing meterable,
  which is the only way to exercise a fallback branch:
  `bestaudio[protocol^=http]/best` passes the first pool, because its
  first branch matches and its unconstrained second branch is never
  reached.

  Both pools carry 1080p, 720p and 480p, so a tier-limited selector is
  judged on its merits rather than failing for want of a format. And a
  `video+audio` selection is judged by its `requested_formats`, not by
  the merged entry, whose own protocol is the parts joined --
  `https+m3u8_native` begins with `http`, which is how an HLS audio
  track once passed for metered.

- `C7.metered` **drives** the `progress_hooks` as a set rather than
  counting them, because `downloader/common.py` calls every hook in
  order with the same status object, so one of them raising is what
  stops the transfer. Requiring each hook to raise individually rejects
  a legitimate `[observer, guard]` pair.

  Four shapes of oversized event are used: length reported, length
  `None`, length absent, and a `finished` event. The first alone is not
  enough -- `total_bytes` is Content-Length, so a hook reading only
  that one passes while being unable to stop a transfer of unknown
  length, which is the case the ceiling exists for.

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
