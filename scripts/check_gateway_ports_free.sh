#!/bin/sh
# Refuse to start a stack while another stack already publishes the gateway
# ports (host ports 80 and 443).
#
# Usage: check_gateway_ports_free.sh <own compose project> [<retry text>]
#        check_gateway_ports_free.sh --list <own compose project>
#
# Exits 0 when no other container publishes host port 80 or 443 (containers of
# the named project are ours and do not count), 1 with a message naming each
# holder otherwise, and 2 on a usage error, when the own project name is empty
# (the caller could not resolve it), or when Docker cannot be asked. The retry
# text (default "run 'make up'") is what the message tells the user to do once
# the ports are free. HERD_GATE_PROJECT, when set, names the gate project THIS
# checkout's `make gate-down` purges; a holder of exactly that name is called
# this checkout's gate stack, and any other `*-gate` holder another checkout's,
# for which the message gives the exact compose command `make gate-down` runs
# (the directory compose last started it from is shown as a hint only: a
# container recreated from another checkout carries that checkout's path).
# With --list it prints one line per holder that is not
# ours ("project <name>" or "container <name>"), nothing else, and exits 0.
#
# Why: the dev stack and the gate stack a `make everything` run leaves serving
# https://localhost publish the same host ports. Starting one while the other
# runs leaves the new Traefik unable to bind (it stays Created) and the new
# stack's services unable to resolve each other, so `make up` fails in a way
# that looks like broken code, and a gate run's tests would talk to the other
# stack's gateway. Only host ports 80 and 443 are checked, by design: they are
# the ones every stack publishes on all interfaces and the ones its clients
# use; a stack that holds only its loopback ports (Postgres, NATS, the
# dashboard) is not seen. Only Docker containers are checked; a host process
# listening on the ports is not seen either.
set -u

list=0
if [ "${1:-}" = "--list" ]; then
    list=1
    shift
fi

if [ "$#" -lt 1 ]; then
    echo "usage: $0 [--list] <own compose project> [<retry command>]" >&2
    exit 2
fi
own=$1
retry=${2:-}
[ -n "$retry" ] || retry="run 'make up'"
if [ -z "$own" ]; then
    echo "Could not determine this checkout's compose project name (docker compose config" >&2
    echo "printed no top-level name); refusing to start rather than guess." >&2
    exit 2
fi

# `docker ps --filter publish=<port>` matches the HOST port. Each line is the
# container's compose project (empty for a container compose did not start),
# its name, and the directory compose was run from, separated by `|`. The
# separator is not white space on purpose: `read` collapses leading IFS white
# space, so with a tab an empty project field would vanish and the container
# name would be read as the project. The working directory comes last, so a
# `|` inside it stays in that field.
holders=""
for port in 80 443; do
    if ! out=$(docker ps --filter "publish=$port" \
            --format '{{.Label "com.docker.compose.project"}}|{{.Names}}|{{.Label "com.docker.compose.project.working_dir"}}'); then
        echo "Could not ask Docker which containers publish port $port." >&2
        exit 2
    fi
    holders="$holders$out
"
done

# One line per distinct holder that is not ours: "project <name>|<working dir>"
# or "container <name>|".
blocking=""
while IFS='|' read -r project name workdir; do
    [ -n "$project$name" ] || continue
    [ "$project" = "$own" ] && continue
    if [ -n "$project" ]; then
        entry="project $project|$workdir"
    else
        entry="container $name|"
    fi
    if ! printf '%s' "$blocking" | grep -Fqx -- "$entry"; then
        blocking="$blocking$entry
"
    fi
done <<HOLDERS
$holders
HOLDERS

if [ "$list" = 1 ]; then
    printf '%s' "$blocking" | sed 's/|.*$//'
    exit 0
fi

if [ -z "$blocking" ]; then
    exit 0
fi

own_gate=${HERD_GATE_PROJECT:-}
echo "Refusing to start compose project $own: another stack already publishes host port 80 or 443."
while IFS='|' read -r holder workdir; do
    [ -n "$holder" ] || continue
    kind=${holder%% *}
    holder=${holder#* }
    if [ "$kind" = "container" ]; then
        echo "  Held by container $holder, which compose did not start. Stop it first."
        continue
    fi
    if [ -n "$workdir" ]; then
        echo "  Held by compose project $holder (started from $workdir)."
    else
        echo "  Held by compose project $holder."
    fi
    case "$holder" in
        *-gate)
            if [ -n "$own_gate" ] && [ "$holder" = "$own_gate" ]; then
                echo "  That is this checkout's seeded gate stack: a successful 'make everything'"
                echo "  left it serving https://localhost, built from this checkout. Either"
                echo "    - use it as is at https://localhost, or"
                echo "    - run 'make gate-down' (this PURGES the gate stack's database), then $retry."
            else
                echo "  That is the seeded gate stack a successful 'make everything' in another"
                echo "  checkout left serving https://localhost. Either"
                echo "    - use it as is at https://localhost, or"
                echo "    - stop it with 'docker compose -p $holder --profile e2e down -v --remove-orphans'"
                echo "      (the teardown 'make gate-down' runs in that checkout; this PURGES the gate"
                echo "      stack's database), then $retry here."
            fi
            ;;
        *)
            echo "  Stop it first, for example with: docker compose -p $holder down"
            ;;
    esac
done <<BLOCKING
$blocking
BLOCKING
exit 1
