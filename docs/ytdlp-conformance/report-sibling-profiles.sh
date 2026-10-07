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
# Report-only is not the same as silent, though. A row says which of
# three things happened -- the profile was read, the repository has not
# declared one, or the query failed -- and a failed query carries the
# error with it. Collapsing those into one row is how a broken token
# comes to look like a sibling that has not adopted the spec.
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

# Fetches one manifest to $1. Returns 0 on success, 3 when the API
# says the path is absent, and 1 for anything else -- which is the
# distinction the first version of this script lost: every failure,
# including an auth failure, was reported as "no manifest found", so a
# repository that had simply not adopted the spec looked identical to
# one the script could not read.
fetch_manifest() {
	local repo="$1" path="$2" target="$3" encoded="" status=0

	encoded="$(gh api "repos/$repo/contents/$path" --jq '.content' 2>"$workdir/err")" ||
		status=$?

	if [ "$status" -ne 0 ]; then
		if grep -qiE 'HTTP 404|Not Found' "$workdir/err"; then
			return 3
		fi
		return 1
	fi

	printf '%s' "$encoded" | base64 -d >"$target" 2>/dev/null || return 1
	[ -s "$target" ] || return 1
	return 0
}

for repo in "${SIBLINGS[@]}"; do
	manifest=""
	diagnosis=""
	for path in "${MANIFEST_PATHS[@]}"; do
		candidate="$workdir/manifest.toml"
		status=0
		fetch_manifest "$repo" "$path" "$candidate" || status=$?
		if [ "$status" -eq 0 ]; then
			manifest="$candidate"
			break
		fi
		if [ "$status" -ne 3 ]; then
			# A real failure, not an absent path. Keep the first one:
			# the later paths will fail the same way.
			diagnosis="${diagnosis:-$(tr -d '\n' <"$workdir/err" | cut -c1-120)}"
		fi
	done

	if [ -z "$manifest" ]; then
		if [ -n "$diagnosis" ]; then
			printf '| %s | unavailable | %s |\n' "$repo" "$diagnosis" >>"$report"
		else
			printf '| %s | no manifest found | - |\n' "$repo" >>"$report"
		fi
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
