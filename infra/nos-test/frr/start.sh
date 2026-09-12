#!/bin/sh
# Entrypoint for the FRR lab node (issue #783 hardening): `set -e` so a
# failure starting sshd stops the container instead of silently falling
# through to docker-start with no SSH access, and an explicit check that
# sshd actually came up (it daemonizes and returns immediately, so its own
# exit code says nothing about whether it is still running a moment later).
set -e

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
