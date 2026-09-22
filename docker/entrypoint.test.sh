#!/bin/bash
# Signal-handling tests for entrypoint.sh.
#
# uvicorn and nginx are replaced by stubs on PATH, so nothing here needs
# the image, a network port, or root. Run: bash docker/entrypoint.test.sh
set -u

# Job control, so the entrypoint runs in its own process group with
# default signal dispositions. Without it a background child of a
# non-interactive shell inherits SIGINT as SIG_IGN and cannot trap it,
# which no container PID 1 ever does -- the SIGINT case then silently
# measured nothing.
set -m

ENTRYPOINT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/entrypoint.sh"
# Every run is bounded: a handler that returns can leave the script
# blocked forever, and that has to surface as a failed check rather
# than a CI job that runs out of time. 124 is timeout(1)'s status.
# -k is required: a handler that returns absorbs timeout's own
# SIGTERM, leaving the wrapper waiting as long as the script.
RUN_TIMEOUT=15
FAILURES=0

setup() {
	WORK="$(mktemp -d)"
	mkdir -p "$WORK/bin"
	for name in uvicorn nginx; do
		cat >"$WORK/bin/$name" <<-STUB
			#!/bin/bash
			echo started >"$WORK/$name.started"
			exec sleep 30
		STUB
		chmod +x "$WORK/bin/$name"
	done
}

teardown() {
	rm -rf "$WORK"
}

fail() {
	echo "  FAIL: $*"
	FAILURES=$((FAILURES + 1))
}

check_exit_code() {
	local expected=$1 actual=$2
	# 124 is timeout(1) reporting the bound; 137 is the SIGKILL that
	# -k delivered when the script absorbed the SIGTERM as well.
	if [[ $actual == 124 || $actual == 137 ]]; then
		fail "the script never exited on its own (killed after ${RUN_TIMEOUT}s)"
		return
	fi
	[[ $actual == "$expected" ]] || fail "expected exit $expected, got $actual"
}

check_not_started() {
	local name=$1
	[[ ! -f "$WORK/$name.started" ]] ||
		fail "$name was started after the stop signal"
}

check_reaped() {
	local pid
	for pid in "$@"; do
		! kill -0 "$pid" 2>/dev/null || fail "child $pid outlived the script"
	done
}

# A signal that arrives once both services are up. This path already
# worked; it is here so the fix cannot change the exit code or start
# leaving children behind.
test_signal_during_normal_operation() {
	local signal=$1 expected=$2
	echo "signal $signal while running -> exit $expected, children reaped"
	setup

	PATH="$WORK/bin:$PATH" REAL_IP_CONF="$WORK/real-ip.conf" \
		timeout -k 2 "$RUN_TIMEOUT" bash "$ENTRYPOINT" >/dev/null 2>&1 &
	local script=$!

	local waited=0
	while [[ ! -f "$WORK/nginx.started" ]] && ((waited < 100)); do
		sleep 0.05
		waited=$((waited + 1))
	done
	[[ -f "$WORK/nginx.started" ]] || fail "the script never finished starting"

	# timeout(1) sits between this job and the script, so the stubs
	# are its grandchildren.
	local children
	children=$(pgrep -P "$(pgrep -P "$script" | head -1)" | tr '\n' ' ')
	kill -"$signal" "$(pgrep -P "$script" | head -1)"
	wait "$script"
	check_exit_code "$expected" "$?"
	# shellcheck disable=SC2086 # deliberate word splitting: a pid list
	check_reaped $children
	teardown
}

# The regression: a signal arriving before the services are up.
#
# REAL_IP_CONF points at a FIFO, so the script blocks in
# write_real_ip_conf -- after the traps are installed and before either
# service starts, which is the window the old handler mishandled.
# Opening a FIFO for writing blocks until a reader appears, and no
# reader ever does. A handler that returns would resume the script here
# and start both services; one that exits cannot.
test_signal_before_the_services_start() {
	echo "SIGTERM during startup -> exit 143, neither service started"
	setup
	mkfifo "$WORK/real-ip.fifo"

	PATH="$WORK/bin:$PATH" REAL_IP_CONF="$WORK/real-ip.fifo" \
		timeout -k 2 "$RUN_TIMEOUT" bash "$ENTRYPOINT" >/dev/null 2>&1 &
	local script=$!

	# The script is blocked on the FIFO from its first write onwards;
	# give it long enough to reach it on a loaded CI runner.
	sleep 1
	kill -TERM "$(pgrep -P "$script" | head -1)"
	wait "$script"
	check_exit_code 143 "$?"
	check_not_started uvicorn
	check_not_started nginx
	teardown
}

test_signal_during_normal_operation TERM 143
test_signal_during_normal_operation INT 130
test_signal_before_the_services_start

if ((FAILURES > 0)); then
	echo "$FAILURES check(s) failed"
	exit 1
fi
echo "all entrypoint signal tests passed"
