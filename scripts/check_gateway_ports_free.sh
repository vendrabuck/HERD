#!/bin/sh
# Refuse to start a stack while another stack already publishes the gateway
# ports (host ports 80 and 443).
#
# Usage: check_gateway_ports_free.sh <own compose project>
#
# Exits 0 when no other container publishes host port 80 or 443 (containers of
# the named project are ours and do not count), 1 with a message naming each
# holder otherwise, and 2 on a usage error or when Docker cannot be asked.
#
# Why: the dev stack and the gate stack a `make everything` run leaves serving
# https://localhost publish the same host ports. Starting one while the other
# runs leaves the new Traefik unable to bind (it stays Created) and the new
# stack's services unable to resolve each other, so `make up` fails in a way
# that looks like broken code. Only Docker containers are checked; a host
# process listening on the ports is not seen here.
set -u

own=${1:-}
if [ -z "$own" ]; then
    echo "usage: $0 <own compose project>" >&2
    exit 2
fi

# `docker ps --filter publish=<port>` matches the HOST port. Each line is the
# container's compose project (empty for a container compose did not start),
# a `|`, and the container's name. The separator is not white space on purpose:
# `read` collapses leading IFS white space, so with a tab an empty project field
# would vanish and the container name would be read as the project.
holders=""
for port in 80 443; do
    if ! out=$(docker ps --filter "publish=$port" \
            --format '{{.Label "com.docker.compose.project"}}|{{.Names}}'); then
        echo "Could not ask Docker which containers publish port $port." >&2
        exit 2
    fi
    holders="$holders$out
"
done

# One line per distinct holder that is not ours: "project <name>" or
# "container <name>".
blocking=""
while IFS='|' read -r project name; do
    [ -n "$project$name" ] || continue
    [ "$project" = "$own" ] && continue
    if [ -n "$project" ]; then
        entry="project $project"
    else
        entry="container $name"
    fi
    if ! printf '%s' "$blocking" | grep -Fqx -- "$entry"; then
        blocking="$blocking$entry
"
    fi
done <<HOLDERS
$holders
HOLDERS

if [ -z "$blocking" ]; then
    exit 0
fi

echo "Refusing to start compose project $own: another stack already publishes host port 80 or 443."
while read -r kind holder; do
    [ -n "$kind" ] || continue
    if [ "$kind" = "container" ]; then
        echo "  Held by container $holder, which compose did not start. Stop it first."
        continue
    fi
    echo "  Held by compose project $holder."
    case "$holder" in
        "$own-gate")
            echo "  That is this checkout's seeded gate stack: a successful 'make everything'"
            echo "  left it serving https://localhost, built fresh from this checkout. Either"
            echo "    - use it as is at https://localhost, or"
            echo "    - run 'make gate-down' (this PURGES the gate stack's database), then 'make up'."
            ;;
        *-gate)
            echo "  That is the seeded gate stack a successful 'make everything' left serving"
            echo "  https://localhost from the checkout directory named ${holder%-gate}. Either"
            echo "    - use it as is at https://localhost, or"
            echo "    - run 'make gate-down' in that checkout (this PURGES the gate stack's"
            echo "      database), then 'make up' here."
            ;;
        *)
            echo "  Stop it first, for example with: docker compose -p $holder down"
            ;;
    esac
done <<BLOCKING
$blocking
BLOCKING
exit 1
