#!/bin/bash
# SSH readiness probe for the SR Linux lab node (issue #1263), run by the
# healthcheck in infra/nos-test/docker-compose.yml after the baseline check.
#
# Why it exists: the baseline check proves sr_cli applied and saved the
# baseline, but says nothing about sshd. A node that passed it could still
# reset the first SSH connection during the banner exchange, and because
# `make nos-up` is `docker compose up --wait`, the first SR Linux dialect test
# then failed with ConnectionResetError on a lab that was "healthy".
#
# What a pass proves: something on port 22 accepted a TCP connection AND sent
# a line starting with "SSH-", the server's version banner. That is the exact
# step that failed: a socket that is listening but resets on connect, or
# closes without a banner, fails this probe, where a bare port check (the
# FRR node's `nc -z`) would pass it.
#
# Where it looks: SR Linux runs its ssh-server inside the network namespace of
# the mgmt network-instance (`srbase-mgmt`), which may not be the namespace
# this healthcheck starts in, and that namespace's loopback may not be up. So
# the probe tries, in order, the srbase-mgmt namespace (when `ip netns exec`
# can enter it) and then the healthcheck's own namespace, and in each one
# 127.0.0.1 and then every global IPv4 address. Any one banner is a pass.
#
# What it relies on in the image: bash with /dev/tcp (start.sh already runs
# under bash), `read -t`, and `ip` from iproute2; `timeout` from coreutils is
# used when present. Without `ip`, or without the srbase-mgmt namespace, it
# degrades to 127.0.0.1 in its own namespace.
#
# Usage: ssh_ready.sh [port]   (the port defaults to 22; the unit tests pass
# their own port to drive the probe against a local fake server)
#
# Each connect is to a local address, so it is normally answered or refused at
# once; a server that accepts but sends nothing is bounded by `read -t`, and
# `timeout` (when the image has it) also bounds a connect that hangs. A failed
# probe exits 1, and Docker retries it on the healthcheck's interval.

set -u

PORT="${1:-22}"
NETNS="srbase-mgmt"
BANNER_WAIT_SECONDS=2
LIMIT=""
if command -v timeout >/dev/null 2>&1; then
    LIMIT="timeout $((BANNER_WAIT_SECONDS + 1))"
fi

# Run in a child bash, so a failed /dev/tcp open ends only that child, and so
# the same text can run inside `ip netns exec`. $1 is the address, $2 the
# port, $3 the seconds to wait for the banner.
# shellcheck disable=SC2016
PROBE='exec 3<>"/dev/tcp/$1/$2" || exit 1
IFS= read -r -t "$3" banner <&3 || exit 1
case "$banner" in
    SSH-*) exit 0 ;;
esac
exit 1'

# Prints 127.0.0.1, then every global IPv4 address, of the namespace the
# given command prefix runs in (an empty prefix means this namespace).
addresses() {
    echo 127.0.0.1
    # shellcheck disable=SC2086
    $1 ip -4 -o addr show scope global 2>/dev/null | while read -r _ _ _ cidr _; do
        echo "${cidr%/*}"
    done
}

# Succeeds when any address of the namespace behind the prefix answers with
# an SSH banner on $PORT.
banner_in() {
    local prefix="$1" addr
    for addr in $(addresses "$prefix"); do
        # shellcheck disable=SC2086
        if $LIMIT $prefix bash -c "$PROBE" ssh_ready "$addr" "$PORT" "$BANNER_WAIT_SECONDS" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

if ip netns exec "$NETNS" true 2>/dev/null; then
    banner_in "ip netns exec $NETNS" && exit 0
fi
banner_in "" && exit 0

echo "ssh_ready.sh: no SSH banner on port $PORT" >&2
exit 1
