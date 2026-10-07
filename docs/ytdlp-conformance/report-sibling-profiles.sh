#!/usr/bin/env bash
# Layer 3 of the conformance drift control: print each public sibling's
# declared spec_version and canonical_region_sha256 into the job summary.
#
# Report-only by design. The caller sets continue-on-error, because a
# pull request in this repository should not be blocked by another
# repository's state; the value is that every run shows these lines, so
# a sibling stuck on an older spec version is visible rather than merely
# discoverable.
#
# Not covered: voice-forge, which is private. Reading it from a public
# repository's CI would mean holding a credential with read access to a
# private repository. It runs layers 1 and 2 in its own CI instead --
# the stronger two -- and the gap is recorded in docs/ytdlp-invariants.md
# so that it stays countable.
#
# This script is not copied downstream; it runs only here.

set -euo pipefail

SIBLINGS=(
	"fdff87554/youtube-downloader"
	"fdff87554/whisper-ui"
)

# Where each repository keeps the manifest that holds the profile. The
# first one that exists wins; this repository's Python lives under
# backend/.
MANIFEST_PATHS=(
	"pyproject.toml"
	"backend/pyproject.toml"
)

# A literal backtick, for the markdown code span around each hash. Held
# in a variable because an inline backtick in a printf format reads as a
# command substitution to shellcheck and to a human skimming the line.
TICK='`'

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Prints "<spec_version>\t<canonical_region_sha256>" for the manifest at
# $1, or a placeholder pair when the profile is absent or unreadable.
#
# The manifest is passed as a path, not on stdin: `python3 -` takes the
# program on stdin, so a heredoc program and piped data cannot coexist.
read_profile() {
	python3 - "$1" <<'PY'
import sys
import tomllib

try:
    with open(sys.argv[1], "rb") as handle:
        data = tomllib.load(handle)
except (OSError, tomllib.TOMLDecodeError) as error:
    print(f"unreadable\t{type(error).__name__}")
    raise SystemExit(0) from None

profile = data.get("tool", {}).get("ytdlp_conformance")
if profile is None:
    print("not declared\t-")
    raise SystemExit(0)

version = profile.get("spec_version", "-")
region = profile.get("canonical_region_sha256", "-")
print(f"{version}\t{region}")
PY
}

# Built up in a file so that the same text reaches two places: the job
# summary, where it renders as a table, and stdout, where it shows up
# for anyone reading the log. The first run of this job wrote only to
# the summary and so printed nothing at all into the log, which made
# "every run shows these lines" true only of the summary page.
report="$workdir/report.md"

{
	printf '## yt-dlp conformance profiles\n\n'
	printf '| repository | spec_version | canonical region |\n'
	printf '| --- | --- | --- |\n'
} >"$report"

for repo in "${SIBLINGS[@]}"; do
	manifest=""
	for path in "${MANIFEST_PATHS[@]}"; do
		candidate="$workdir/manifest.toml"
		if gh api "repos/$repo/contents/$path" --jq '.content' 2>/dev/null |
			base64 -d >"$candidate" 2>/dev/null && [ -s "$candidate" ]; then
			manifest="$candidate"
			break
		fi
	done

	if [ -z "$manifest" ]; then
		printf '| %s | no manifest found | - |\n' "$repo" >>"$report"
		continue
	fi

	IFS=$'\t' read -r version region < <(read_profile "$manifest")
	printf '| %s | %s | %s |\n' \
		"$repo" "$version" "${TICK}${region:0:12}${TICK}" >>"$report"
done

printf '\nReport only. See docs/ytdlp-invariants.md for why voice-forge is absent.\n' \
	>>"$report"

cat "$report"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
	cat "$report" >>"$GITHUB_STEP_SUMMARY"
fi
