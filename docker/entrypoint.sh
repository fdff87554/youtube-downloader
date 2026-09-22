#!/bin/bash
set -e

# Overridable so the signal tests can point it at a FIFO and hold the
# script inside write_real_ip_conf; nginx.conf includes the default.
REAL_IP_CONF="${REAL_IP_CONF:-/tmp/nginx-real-ip.conf}"

# Teach nginx which upstream proxies may speak for the client.
#
# Without this, every request behind an external reverse proxy carries
# that proxy's address as $remote_addr, so both nginx's limit_req zone
# and slowapi's per-IP key collapse onto a single value and one abusive
# client rate-limits everyone. nginx.conf includes this file
# unconditionally, so it is written (possibly empty) on every start.
write_real_ip_conf() {
	: >"$REAL_IP_CONF"
	if [[ -z ${TRUSTED_PROXIES:-} ]]; then
		return 0
	fi
	local cidr
	local IFS=','
	for cidr in $TRUSTED_PROXIES; do
		cidr="${cidr//[[:space:]]/}"
		if [[ -n $cidr ]]; then
			printf 'set_real_ip_from %s;\n' "$cidr" >>"$REAL_IP_CONF"
		fi
	done
	printf 'real_ip_header X-Forwarded-For;\nreal_ip_recursive on;\n' \
		>>"$REAL_IP_CONF"
}

# Forward SIGTERM/SIGINT to the children so `docker stop` exits cleanly
# instead of waiting for the stop timeout. Invoked only through the trap
# below, which shellcheck cannot see.
# shellcheck disable=SC2329
shutdown() {
	kill -TERM "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true
	wait "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true
}

# The signal traps exit instead of tearing down and returning. A handler
# that returns hands control back to the line after the one the signal
# interrupted, so a stop arriving during startup tore down whatever
# existed and then went on to start the rest: measured at 6 of 10 runs,
# nginx came up after the handler had already finished. A signal
# arriving earlier still, before UVICORN_PID is set, would kill nothing
# at all and leave the script blocking in `wait -n` on two healthy
# services until the stop timeout SIGKILLed the container -- that one is
# read off the code, not measured; the window is too narrow to hit
# deliberately.
#
# Exiting routes teardown through the EXIT trap, which nothing can
# follow. 128+signo is the conventional status for a signal-terminated
# process, and 143 is what this script already returned for a SIGTERM
# during normal operation, so the stop path keeps its exit code.
trap 'exit 143' SIGTERM # 128 + SIGTERM (15)
trap 'exit 130' SIGINT  # 128 + SIGINT (2)
trap shutdown EXIT

# After the traps, so a stop during config writing gets the handler
# rather than bash's default disposition.
write_real_ip_conf

# --forwarded-allow-ips stays at the loopback nginx connects from: nginx
# is the only process that may set X-Forwarded-For here, and it appends
# the address it trusts (see write_real_ip_conf). Widening this to "*"
# would let any client forge the rate-limit key.
uvicorn --factory app.main:create_app \
	--host 127.0.0.1 \
	--port 8000 \
	--workers 1 \
	--proxy-headers \
	--forwarded-allow-ips 127.0.0.1 \
	--log-level warning &
UVICORN_PID=$!

nginx -g "daemon off;" &
NGINX_PID=$!

# Exit as soon as either process dies, then make sure the other one is
# torn down before the container exits so we don't leak orphans.
#
# `|| EXIT_CODE=$?` is what makes the teardown reachable: under `set -e`
# a non-zero `wait -n` aborted the script on the spot, so neither the
# assignment nor the shutdown below ever ran -- including on the one
# path that needs them, uvicorn refusing to start without
# ALLOWED_ORIGINS. The EXIT trap covers the remaining early exits.
wait -n || EXIT_CODE=$?
exit "${EXIT_CODE:-0}"
