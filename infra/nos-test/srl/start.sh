#!/bin/bash
# Boots the SR Linux management daemon, waits until sr_cli answers, applies
# the checked-in CLI baseline (baseline.cli) exactly once per boot, then
# waits on the daemon so it stays the container's PID 1 workload.
#
# Why a baseline is needed at all: a factory-fresh SR Linux node's config
# prompt reads "--{ [FACTORY] + candidate private private-admin }--", and
# netmiko 4.7.0's nokia_srl.check_config_mode regex expects the mode marker
# immediately after "--{", so the "[FACTORY]" token makes send_config_set
# fail with "Failed to enter configuration mode." Running `save startup`
# once clears the [FACTORY] tag; after that, netmiko works unchanged. So
# this lab must apply and save a baseline at every boot (the container is
# stateless, see the docker-compose.yml header comment), not just once ever.
#
# Idempotent by construction: re-running the same `set` commands against an
# already-configured node and committing again is harmless (SR Linux commits
# a no-op diff cleanly), so this script does not need to detect prior state.

set -u

/opt/srlinux/bin/sr_linux &
SRL_PID=$!

echo "start.sh: waiting for sr_cli to answer..."
# NOTE: sr_cli's "-c" flag is NOT "run this command"; it is
# "--commit-at-end" (auto-appends "commit now" after the given command).
# Passing a command as a bare positional argument, with no -c, is a
# single-shot, non-interactive invocation and the correct form for both
# read-only probes and (when the command changes candidate config) explicit
# scripted commits. Using -c here made every invocation exit 1 with
# "Parsing error: Unknown token 'commit'" (the auto-commit is invalid
# outside candidate mode), which hung this very loop forever; verified live.
until sr_cli "show version" >/dev/null 2>&1; do
    sleep 1
done
echo "start.sh: sr_cli answers, applying baseline"

# sr_cli answering "show version" does not mean the config/mgmt subsystem is
# ready: verified live, an early "show version" can succeed while piping the
# baseline still fails with "Error: Server is starting". So retry the
# baseline apply itself rather than treating one failure as fatal; this is
# also why the script does not use "set -e" (a transient failure here must
# not kill the container, since this script is PID 1's child process tree).
until sr_cli < /etc/opt/srlinux/baseline.cli > /tmp/baseline-apply.log 2>&1; do
    echo "start.sh: baseline apply not ready yet, retrying in 2s"
    cat /tmp/baseline-apply.log
    sleep 2
done
echo "start.sh: baseline applied"
cat /tmp/baseline-apply.log

wait "$SRL_PID"
