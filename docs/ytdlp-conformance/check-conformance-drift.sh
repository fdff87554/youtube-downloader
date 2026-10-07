#!/usr/bin/env bash
# Drift control for the copied yt-dlp conformance test.
#
# Two checks, both local and both offline:
#
#   1. The canonical region of the copied test still hashes to the
#      value recorded in [tool.ytdlp_conformance].canonical_region_sha256.
#      The region is hashed rather than the whole file because additions
#      below the end sentinel are legitimate, and a check that cries
#      wolf teaches people to ignore it.
#   2. The number of xfail markers in the copied test is within
#      xfail_budget. The xfail list is the backlog and may only shrink.
#
# Copy this next to the test and run it from the repository root in CI.
# Spec: docs/ytdlp-invariants.md in fdff87554/youtube-downloader.
#
# Usage: check-conformance-drift.sh [test-path] [manifest-path]

set -euo pipefail

TEST_PATH="${1:-tests/test_ytdlp_conformance.py}"
MANIFEST="${2:-pyproject.toml}"

BEGIN_SENTINEL='# --- ytdlp-conformance:canonical-begin ---'
END_SENTINEL='# --- ytdlp-conformance:canonical-end ---'

die() {
	printf 'conformance drift: %s\n' "$1" >&2
	exit 1
}

[ -f "$TEST_PATH" ] || die "no conformance test at $TEST_PATH"
[ -f "$MANIFEST" ] || die "no manifest at $MANIFEST"

# tomllib rather than grep: the profile is TOML, and a key matched by
# regex would also match one inside a comment or a different table.
read_profile_key() {
	python3 - "$MANIFEST" "$1" <<'PY'
import sys
import tomllib

manifest, key = sys.argv[1], sys.argv[2]
with open(manifest, "rb") as handle:
    profile = tomllib.load(handle).get("tool", {}).get("ytdlp_conformance")
if profile is None:
    sys.exit(f"{manifest} declares no [tool.ytdlp_conformance]")
if key not in profile:
    sys.exit(f"{manifest} declares no {key}; every profile key is mandatory")
print(profile[key])
PY
}

expected_hash="$(read_profile_key canonical_region_sha256)"
xfail_budget="$(read_profile_key xfail_budget)"

# Inclusive of both sentinels, so moving a sentinel is itself drift.
region="$(
	awk -v b="$BEGIN_SENTINEL" -v e="$END_SENTINEL" '
		$0 == b { inside = 1 }
		inside  { print }
		$0 == e { inside = 0; found = 1 }
		END     { if (!found) exit 3 }
	' "$TEST_PATH"
)" || die "no canonical region in $TEST_PATH; both sentinels must be present"

actual_hash="$(printf '%s\n' "$region" | sha256sum | cut -d' ' -f1)"

if [ "$actual_hash" != "$expected_hash" ]; then
	die "$(
		cat <<MSG
the canonical region of $TEST_PATH has changed.
  recorded: $expected_hash
  actual:   $actual_hash
Repository-specific tests belong BELOW the end sentinel. If the region
itself needs to change, change it upstream in
docs/ytdlp-conformance/ first, then re-copy here and update
canonical_region_sha256 in $MANIFEST -- that line moving is the visible
part of this mechanism, not a formality to clear.
MSG
	)"
fi

# Matches the marker, not the word: "xfail_budget" appears in the
# test's own list of profile keys, and a bare grep for xfail counted it.
xfail_count="$(grep -cE 'pytest\.mark\.xfail|^[[:space:]]*@.*\bxfail\b' \
	"$TEST_PATH" || true)"

if [ "$xfail_count" -gt "$xfail_budget" ]; then
	die "$(
		cat <<MSG
$TEST_PATH holds $xfail_count xfail marker(s), over a budget of $xfail_budget.
The xfail list is the backlog and may only shrink. Fix the invariant,
or -- if the gap is genuinely accepted -- record a waiver in
$MANIFEST instead, which has to cite an issue.
MSG
	)"
fi

printf 'conformance: region %s, %s/%s xfail\n' \
	"${actual_hash:0:12}" "$xfail_count" "$xfail_budget"
