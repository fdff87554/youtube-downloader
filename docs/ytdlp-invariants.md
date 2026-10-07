# yt-dlp Integration Invariants

Three self-hosted projects drive yt-dlp: this one (interactive HTTP
streaming), Whisper-UI (batch transcription) and voice-forge (corpus
collection). They deliberately do **not** share a Python package -- the
reasoning, and the conditions under which that should be revisited, are
under [Exit criteria](#exit-criteria). What they share instead is this
document plus a conformance test that each repository copies and runs
offline in its own CI.

This file is the canonical copy. The other two repositories do not
duplicate it; they link here from their own conformance test.

## What this spec binds, and what it does not

An invariant here is a property that must hold in all three
repositories. A **value** usually is not an invariant. The clearest
example is the extractor pin: `allowed_extractors` is `["youtube.*"]`
here and `["youtube"]` in the other two, and both are correct, because
this project accepts a URL as the user typed it (`?list=`, `/clip/`)
while the other two canonically rebuild it to
`https://www.youtube.com/watch?v=<id>` first. The invariant is "a pin
exists, is non-empty, and its accept/reject matrix matches what this
repository declares" -- not any particular pin.

Everything that legitimately differs is a **profile key**, listed under
[Profile keys](#profile-keys). Every key is mandatory and has no
default, so that adding a key upstream turns all three repositories red
at once instead of passing silently in two of them.

## How enforcement is split

- **Layer 1, this document.** Reasons, measurements, rejected
  alternatives. Read by humans; never loaded into an agent's context
  automatically.
- **Layer 2, `docs/ytdlp-conformance/`.** A test each repository copies.
  The expected values are data (the profile); the test logic is a
  constant. Runs offline.
- **Layer 3, runtime self-check.** Catches a deployment that has drifted
  from the code it was built from.

Layer 2 is the only mechanical layer. A rule file under `.claude/rules/`
was considered and rejected for the enforcement chain: `.claude/` is
untracked in this repository and in Whisper-UI, being listed in
`.git/info/exclude`, so a rule file there is invisible to the
repository, unreviewable in a pull request, and cannot be hashed by a
check running in CI. Only voice-forge tracks `.claude/`. A local rule
file remains a useful convenience, but it cannot carry an invariant.

## Spec version

```text
SPEC_VERSION = 1.0.0
```

The conformance test declares this same constant and asserts it equals
the `spec_version` in its repository's profile. Bumping the spec
therefore turns every repository red until its profile is updated, which
is the point: the red is the migration notice.

## The invariants

- **C1 -- the extractor pin.** A pin exists, is non-empty, and its
  accept/reject matrix matches the declared `extractor_spec`. Violated:
  extraction falls back to `GenericIE`, which follows off-site
  redirects.
- **C2 -- the challenge solvers.** The installed yt-dlp-ejs version
  equals the version the installed yt-dlp pins. Declaring "not needed"
  requires asserting it is **not** installed. Violated: signature and
  n-challenge solving fail, silently.
- **C3 -- the JS runtime.** Its availability is _declared_, not
  discovered at runtime. Violated: the `web` client drops out of the
  defaults and the symptom is swallowed by `quiet`.
- **C4 -- control characters.** Remote-controlled strings are
  neutralised before they are displayed or logged. Violated: forged
  output lines. Reproduced in voice-forge.
- **C5 -- error messages.** They carry no internal detail: no
  filesystem paths, no private IPs, no cookie file names. Violated:
  information disclosure.
- **C6 -- URL rebuilding.** A URL is canonically rebuilt before it
  reaches yt-dlp, and the rebuilt result passes this repository's own
  accept matrix. Violated: `@evil.com` authority confusion, a smuggled
  `list=`.
- **C7 -- the byte ceiling.** A media stream has one, and it holds on
  every downloader path. Violated: disk or memory exhaustion.
- **C8 -- the time bound.** A download has one, and the spec does not
  claim `socket_timeout` is it. Violated: a transfer that never
  finishes and never fails.
- **C9 -- yt-dlp's own output.** The Python API path supplies a
  `logger`; the CLI path captures stderr. Violated: `quiet` does not
  suppress `trouble()`, so warnings vanish instead of being logged.
- **C10 -- error-text coupling.** Any coupling to yt-dlp's error text
  is pinned to a yt-dlp version. Violated: silent rot. Already happened
  in Whisper-UI.

`subprocess` list form and the absence of `shell=True` are deliberately
**not** tested. They are enforced by ruff `S602`, `S604` and `S605`,
which all three repositories now select. `S603` is deliberately left
out: it fires on every list-form call too, which would mean a `noqa` on
each one and the habit of adding more.

### Why C7 needs more than max_filesize

`max_filesize` is not a byte ceiling. In `yt_dlp/downloader/http.py`
(2026.08.19, lines 216-227) it is consulted once, before the transfer
starts, only when `Content-Length` is known, and it returns `False`
rather than raising. A chunked response, or any downloader that does
not meter, walks straight past it. C7 therefore needs three layers, and
a repository that claims C7 with only `max_filesize` does not conform:

1. Make unmeterable downloaders unselectable, **on every fallback
   branch**. A selector with no protocol constraint does not merely
   allow an unmeterable format, it can prefer one: measured against
   yt-dlp's own `build_format_selector`, `bestaudio/best` picked
   `hls-ffmpeg` (protocol `m3u8`), while adding `[protocol^=http]` to
   both branches picked `https-a`.

   The mechanism, which is what makes this general rather than one
   observation: `proto` sits late in yt-dlp's default sort order,

   ```text
   hasaud, lang, quality, tbr, filesize, vbr, height, width, proto, ...
   ```

   so protocol only breaks a tie once bitrate, filesize and resolution
   are equal. Comparing two renditions that are identical in all of
   those will show `https` winning and invite the conclusion that the
   unmeterable path is unreachable. Give the HLS rendition a higher
   `tbr` and it wins, under a project's own `format_sort` too. "Only
   when nothing else is offered" is the wrong reading, and a
   measurement on an equal-quality pair is how one arrives at it.

   This also means the constraint has to be checked by **evaluating**
   each selector, not by looking for the text of a protocol filter:
   `bestaudio[protocol^=http]/best` contains one and still falls back
   to an unconstrained branch, and `best[protocol=m3u8]` contains one
   that selects exactly the wrong thing.

2. Meter mid-transfer in a `progress_hooks` callback, keyed per output
   filename so the count survives a retry.
3. Verify the landed file on disk and refuse to publish it when it is
   over the ceiling. A staging directory plus publish-after-validation
   is required: without it an oversized download still becomes cache.

Layers 2 and 3 are both needed because an external downloader reports
no progress. `FFmpegFD` emits a single `finished` event when its child
exits, and two routes reach it: protocol `m3u8` maps to it directly,
and `m3u8_native` hands it the _whole_ transfer whenever `HlsFD` cannot
handle the manifest itself (`downloader/hls.py`, `can_download` ->
`FFmpegFD`) -- a non-AES-128 `#EXT-X-KEY`, or AES-128 with ffmpeg
present and pycryptodomex absent.

### Why C8 does not mean socket_timeout

An unset `socket_timeout` is not an absent timeout:
`yt_dlp/networking/common.py:34` sets `DEFAULT_TIMEOUT = 20` and `:242`
applies it. It is a per-operation socket timeout, though, not a bound
on the whole download, and a spec that conflates the two invites
exactly the wrong fix.

### Three kinds of retry, and the one that defaults to nothing

yt-dlp has three retry budgets, and they are not interchangeable. The
distinction matters because `retries` looks like the general one and is
not.

- **Extraction** uses `extractor_retries`, which
  `extractor/common.py:4072` defaults to 3:
  `RetryManager(self.get_param('extractor_retries', 3), ...)`. The
  YouTube extractor uses it in `youtube/_base.py:981`
  (`_download_webpage_with_retries`) and `:1287-1289`
  (`_extract_response`, the API JSON path behind player responses and
  playlist browsing). A caller that sets nothing therefore still gets
  four attempts at metadata.
- **Media download** uses `retries`, read by
  `downloader/http.py:360` as `RetryManager(self.params.get('retries'), ...)`.
- **Fragment download** uses `fragment_retries`, the same story per
  fragment.

The last two default to 10 **only in the CLI option parser**
(`options.py:1025`). A directly constructed `YoutubeDL(params)` gets
`None`, and `utils/_utils.py:5267` turns that into `0`. Measured:

```text
params.get('retries')                        -> None
RetryManager(retries=None)                   -> 1 attempt
get_param('extractor_retries', 3)            -> 3
RetryManager(extractor_retries=3)            -> 4 attempts
```

So an in-process caller that never sets `retries` has no retry **on the
media transfer**: one transient failure fails that download. Its
metadata extraction is unaffected, and a repository that drives yt-dlp
in-process purely to read metadata needs neither key.

C8 therefore constrains `retries` and `fragment_retries` at download
call sites only. Setting them on an extract-only call site would be
harmless but meaningless, and requiring it would be a spec asserting a
defect that is not there. What does apply everywhere: if a repository
sets `extractor_retries` at all, it must be positive -- setting it to 0
silently removes the retry that was there by default.

### Why C10 pins to a version

Matching on yt-dlp's error text is sometimes the only option, but the
text is not API. Any such coupling records the version it was verified
against in `markers_verified_against`, and the conformance test fails
once the installed version no longer matches, so the coupling gets
re-verified deliberately instead of being discovered in production.

## Profile keys

Declared per repository in `[tool.ytdlp_conformance]`. Every key is
mandatory; none has a default.

- `spec_version` -- must equal the `SPEC_VERSION` above.
- `canonical_region_sha256` -- sha256 of the canonical region of the
  copied test; see [Drift control](#drift-control).
- `extractor_spec` -- the `allowed_extractors` value in use.
- `accepts_urls`, `rejects_urls` -- the accept/reject matrix this
  repository claims for its pin. C1 is "the matrix matches what this
  repository declares", so the declaration has to be data, not prose.
  C6 reuses the same two lists: an accepted URL must survive the
  canonical rebuild and still pass the pin, and a rejected one must be
  refused by the rebuild **or** by the pin. The pair is the invariant,
  not either layer alone -- measured here, the URL check passes
  `youtube.com/about/` through on purpose, because this project accepts
  the URL as the user typed it, and the pin is what matches no
  extractor for it. What must never happen is a declared rejection
  reaching an extractor.
- `cachedir` -- `false`, or the path. Not unified; see
  [Evaluated and not adopted](#evaluated-and-not-adopted).
- `codec_policy` -- `h264-preferred`, `h264-forced`, `audio-only`, or
  `none`.
- `socket_timeout_seconds` -- the explicit value. Never left implicit.
- `max_bytes` -- the C7 ceiling, in bytes.
- `js_runtime` -- the runtime name, or `absent`.
- `js_runtime_provided_by` -- `image`, `host`, or `none`. A declared
  runtime the repository does not itself install is a deployment
  dependency, and saying so out loud is the point of the key.
- `ejs_coupling` -- `pinned` or `absent`. `absent` requires the reverse
  assertion in C2.
- `markers_verified_against` -- the yt-dlp version any error-text
  coupling was verified against, or `none`.
- `po_token_hook` -- `absent` or `env`; see
  [PO Token mount points](#po-token-mount-points).
- `player_clients` -- the declared client list, today `["default"]` in
  all three.
- `needs_ffmpeg`, `needs_ffprobe` -- whether the repository requires
  each binary.
- `waivers` -- a table of named deviations, keyed by aspect; see
  [Aspects](#aspects) and [Waivers](#waivers) below.
- `xfail_budget` -- the maximum number of **collected test items in
  the conformance module that carry an `xfail` marker**. Items, not
  marker expressions: one `pytestmark` neutralises a whole module, and
  counting expressions would call that one.

### Aspects

Two of the invariants are not one check. C7 asks for a ceiling that
holds on _every_ downloader path, and C8 for a bound on the whole
download; each decomposes, and a test that covers one part must not be
counted as covering the invariant. So the accounting unit is an
**aspect**:

- `C7.selectable` -- no format selector, on any fallback branch, can
  choose an unmeterable downloader.
- `C7.metered` -- a transfer driven in-process is observed while it
  runs, and exceeding the ceiling stops it.
- `C7.external` -- a transfer driven as a subprocess has a byte bound
  too. The shared test cannot observe one, so this aspect exists to be
  waived, by name, by any repository that downloads through a
  subprocess.
- `C7.landed` -- the finished bytes are measured before they are
  published or served.
- `C8.socket` -- the per-operation socket timeout is declared, not
  inherited.
- `C8.retries` -- download call sites set `retries` and
  `fragment_retries`.
- `C8.deadline` -- the whole download has a wall-clock bound.

The other invariants are single aspects and are referred to by their
bare ID. The conformance test declares which aspects it checks
mechanically; every aspect it does not check must be waived.

Three are **not** mechanically checkable from the shared adapter
surface today, so every repository waives them explicitly:

- `C5` -- error-message content is not visible through the adapter.
- `C7.landed` -- a repository that streams its media never lands a
  file, and one that does exposes no hook for the shared test to
  inspect.
- `C8.deadline` -- none of the three has a wall-clock bound today, and
  a shared test cannot invent the mechanism it would check.

`C7.external` joins them for any repository with a subprocess download
site, which is every repository here except on its in-process paths.
The reason it is a waiver rather than a check: the only thing visible
from an argv is `--max-filesize`, and
[that is not a ceiling](#why-c7-needs-more-than-max_filesize) -- it is
consulted once, pre-transfer, only with a known `Content-Length`.
Asserting its presence would record conformance that does not exist,
which is worse than recording the gap. Until the adapter can observe a
subprocess transfer, the honest answer is a named waiver with an issue
behind it.

Splitting `C7.external` out rather than widening `C7.metered` is
deliberate: a repository that meters its in-process downloads correctly
and also shells out for one keeps the credit for the part it does, and
waives only the part nobody can see.

Naming them here, rather than leaving them off the list, is the point:
an aspect that nobody has to waive is an aspect nobody counts.

### Waivers

A repository that cannot satisfy an aspect does not silently omit it.
It records a waiver keyed by aspect, with a named reason and an issue
link:

```toml
[tool.ytdlp_conformance.waivers]
C3 = "deno comes from the container image; see youtube-downloader#NNN"
"C8.deadline" = "no wall-clock bound on a streamed response; see #NNN"
```

Waivers are visible, countable and greppable, which is the whole
requirement. An aspect with no waiver and no passing test is a
failure, not a gap -- and because the aspect list lives here rather
than in each repository, adding one upstream turns all three red until
each decides whether to implement it or waive it.

## Drift control

Three layers, in decreasing strength.

1. **Spec version coupling.** The test's `SPEC_VERSION` must equal the
   profile's `spec_version`. Cheap, and it makes an upstream bump
   impossible to ignore.
2. **Region-scoped sha256.** Two sentinel lines delimit the canonical
   region of the copied test. `check-conformance-drift.sh` recomputes
   that region's hash and compares it to `canonical_region_sha256` in
   the profile, which lives outside the region. The region is hashed
   rather than the whole file because additions below the end sentinel
   are legitimate. Measured: of nine copied `.claude/rules/` files
   across these three repositories, only two were byte-identical to
   upstream, and at least one difference was a valid downstream
   addition, which a whole-file hash cannot tell apart from rot. A
   check that cries wolf trains people to ignore it.
3. **Cross-repository report.** This repository's CI reads the other
   public repository's `spec_version` and `canonical_region_sha256` and
   prints them into the job summary. It is **report-only** and never
   fails: a pull request here should not be blocked by another
   repository's state, but every pull request here shows those lines.

**Known gap, stated so that it stays countable:** layer 3 covers this
repository and Whisper-UI. voice-forge is private, and reading it from
here would mean a public repository's CI holding a credential with read
access to a private repository. That was judged the wrong trade against
least privilege, so voice-forge is covered by layers 1 and 2 -- which
run inside its own CI and are the strong ones -- plus manual review.

The `xfail` list is the backlog, and it may only shrink: the budget is
enforced as a test, which fails when more collected items in the
conformance module carry an `xfail` marker than `xfail_budget` allows.
Lowering the budget is a visible one-line diff, and so is raising it,
which is the intent. The original plan compared against the previous
commit; a recorded budget replaces that, because CI clones are shallow
and a check that cannot run is not a check.

**The budget is counted from pytest's collection, not from the file's
text.** A text scan cannot see what pytest sees, and the gap is not
theoretical: two lines outside the hashed region --

```python
from pytest import mark

pytestmark = mark.xfail(strict=False, reason="...")
```

-- turn every test in the module into an `xfail`, while a `grep` for
`pytest.mark.xfail` finds nothing. Measured against an earlier
text-scanning version: it reported `0/0 xfail` and exited 0 while
pytest reported `3 skipped, 4 xfailed, 21 xpassed`. The whole
conformance suite was neutralised and the drift check called it clean.
Reading the collected markers covers that, aliased imports, and
markers applied from a `conftest.py`.

## Measurement record

Every number below was measured, not derived. Dates and versions matter
because none of it is a general truth about YouTube.

### What was measured

- **The extractor matrix**, against the real extractor registry: all
  three of `["youtube"]`, `["youtube","youtube:tab"]` and
  `["youtube.*"]` reject `/about/`,
  `youtube.com@evil.com/watch?v=ID` and the link-local metadata
  address. They differ on `youtu.be/ID?list=`, `/clip/` and
  `/playlist?list=`, which is exactly why the pin value is a profile
  key and not an invariant.
- **Format selection**, one video, yt-dlp 2026.08.19: an
  unconstrained `[ext=mp4]` preference selected format `401` (AV1,
  2160p) at 232.5 MiB, where adding a codec sort selected `137`
  (H.264, 1080p) at 80.5 MiB, and `[height<=720]` selected `136` at
  28.5 MiB. The 232.5 MiB was being fetched to produce a 16 kHz mono
  wav.
- **Retry defaults**, quoted under
  [Three kinds of retry](#three-kinds-of-retry-and-the-one-that-defaults-to-nothing).
- **Protocol preference**, quoted under
  [Why C7 needs more than max_filesize](#why-c7-needs-more-than-max_filesize).

### What was not measured

- PO Token and JS runtime behaviour was observed on **one video, one
  IP, one point in time**. It does not generalise and must not be
  quoted as though it did.
- "Disabling `cachedir` raises bot-detection risk" is an inference from
  request volume and player-JS refetching. It was not measured.
- Whether `visionos`, one of the current default clients, needs a GVS
  PO Token: the official wiki lists it in neither the "needs" nor the
  "does not need" table. Unresolved.
- Whether format-selection semantics are identical between yt-dlp
  2026.06.09 and 2026.08.19. The AV1-preference conclusion held in
  both, but the full semantics were not compared.
- `_ies` and `_js_runtimes` are private attributes. The conformance
  test guards them with `hasattr` and **fails** rather than skips when
  they are missing, because a silent skip is the failure mode this
  whole document exists to prevent.

## Evaluated and not adopted

- **`cachedir=False` as a common default.** Buys nothing for the batch
  consumers; only adds nsig recomputation.
- **Forcing H.264 everywhere.** A filter makes an AV1-only video fall
  back to a worse progressive format. Preferring via sort is correct;
  forcing is not. Irrelevant to the audio-only consumer.
- **Sharing Whisper-UI's error-marker retry.** The interactive HTTP
  consumer has a human who retries, and the corpus consumer is a single
  CLI run. Copying it only widens the coupling to yt-dlp's text.
- **Removing voice-forge's explicit `js_runtimes`.** It is the only one
  of the three that got this right; the direction should be the
  reverse.
- **Playlist support in voice-forge.** Narrowing the attack surface is
  a positioning decision, not an oversight.
- **Installing a PO Token provider now.** Not needed as measured. It is
  an extra Node or Deno service, which for a read-only container that
  writes nothing to disk is an architectural change, not a dependency.
  Mount points are reserved instead.
- **Pinning ffmpeg.** All three use stable flags. CI writes the first
  line of `ffmpeg -version` into the log instead, so that a bisect has
  something to stand on.
- **Extracting a shared Python package.** See
  [Exit criteria](#exit-criteria).

### PO Token mount points

Not installed, but four places are reserved so that the day it is
needed is a configuration change rather than a redesign:

- `po_token_hook` is a profile key. If `YTDLP_PO_TOKEN` is set in the
  environment while the profile still says `absent`, the test fails --
  which catches a half-applied configuration.
- `YTDLP_PO_TOKEN` is the shared environment contract across all three,
  holding yt-dlp's own `po_token` format (`web.gvs+XXX`, comma
  separated). The Python API path passes
  `opts["extractor_args"] = {"youtube": {"po_token": ...}}`; the CLI
  path passes `--extractor-args "youtube:po_token=..."`.
- `player_clients` is a declared value. When PO Token pressure appears,
  the first response is to switch to a client that does not need one
  (`tv`, `web_embedded`, `android_vr`), not to go and obtain a token.
- Bot detection gets its own error code in the error mapping. Without
  one, the day it arrives looks like an HTTP 500.

## Exit criteria

The three repositories copy a test instead of importing a package. The
reasons: this repository requires Python 3.14 because it relies on
PEP 758, while voice-forge is held below 3.13 by torch, so a shared
package's floor would be set by a third party; the genuinely duplicated
surface is about 165 lines out of roughly 2372 yt-dlp-related lines, or
7%; this repository installs with `--require-hashes` and runs
`pip-audit --strict`, so a non-PyPI dependency is not installable and a
PyPI 404 is fatal, which means the clean solution would be publishing a
public package and taking on a permanent external obligation; and a
shared enum turns three independent small failures into one
simultaneous failure.

Copying also has a measured bad record, stated plainly: of the nine
copied rule files across these repositories, seven had drifted. The
distinction this spec rests on is that a stale `CLAUDE.md` never turns
anything red, while a stale conformance test still runs, and still
fails when someone removes the extractor pin. It degrades to "the old
protection", not to "no protection". A shared package would instead
get Dependabot bumps and a recorded lock version, a mechanism that does
not depend on human discipline. **If the drift report is not going to
be read, the package route is the correct one.**

Revisit this decision when any of the following becomes true:

- a fourth consumer appears;
- the shared surface exceeds roughly 200 lines;
- PO Token becomes mandatory, which would be 300+ lines of logic with
  identical reasons to change in all three.

The three pieces that already have identical reasons to change -- the
control-character neutralisation (C4), the canonical URL rebuild (C6)
and the PO Token mount points -- total under 60 lines today. They are
what to measure against that 200-line threshold.

## Applying this spec to a repository

See `docs/ytdlp-conformance/README.md` for the copy targets, the
sentinel conventions, and how to remove the whole mechanism again.
