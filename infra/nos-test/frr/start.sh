#!/bin/sh
# Entrypoint for the FRR lab node (issue #783 hardening): `set -e` so a
# failure starting sshd stops the container instead of silently falling
# through to docker-start with no SSH access, and an explicit check that
# sshd actually came up (it daemonizes and returns immediately, so its own
# exit code says nothing about whether it is still running a moment later).
set -e

# VRF fixture (ADR 0014 addendum X-G, issue #755): a deterministic Linux VRF the
# Layer 3 driver tests can install a route into, the way the SR Linux node
# carries its checked-in baseline. `blue` maps to routing table 10 and owns one
# member interface, a dummy at 192.0.2.254/30 (RFC5737 TEST-NET-1, the same
# range the live route tests draw their destination prefixes from).
#
# Why a VRF fixture has to exist at boot: FRR accepts `ip route <prefix>
# <next_hop> vrf <name>` into its configuration whether or not the Linux VRF
# device exists, but without the device it never installs the route, answering
# "Static Route to <prefix> not installed currently because dependent config not
# fully available" and "% VRF <name> not active". So a lab with no VRF can only
# ever prove the failure case.
#
# Created before sshd so the device is present the moment the node is reachable,
# and idempotently (each step skipped when it already exists) so a container
# restart re-enters this cleanly. Never deleted: FRR refuses `no vrf <name>`
# with "% Only inactive VRFs can be deleted" while the Linux device exists, so
# tests treat the fixture as permanent and clean up only their own routes.
if ! ip link show blue >/dev/null 2>&1; then
    ip link add blue type vrf table 10
fi
ip link set blue up
if ! ip link show dummy0 >/dev/null 2>&1; then
    ip link add dummy0 type dummy
fi
ip link set dummy0 master blue
ip link set dummy0 up
if ! ip -4 -o addr show dev dummy0 | grep -q "192.0.2.254/30"; then
    ip addr add 192.0.2.254/30 dev dummy0
fi

/usr/sbin/sshd

sshd_up=0
i=0
while [ "$i" -lt 5 ]; do
    if pgrep sshd >/dev/null 2>&1; then
        sshd_up=1
        break
    fi
    sleep 1
    i=$((i + 1))
done
if [ "$sshd_up" -ne 1 ]; then
    echo "start.sh: sshd did not start" >&2
    exit 1
fi

exec /usr/lib/frr/docker-start
