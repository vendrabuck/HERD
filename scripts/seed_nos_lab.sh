#!/usr/bin/env bash
# Seed a running HERD stack with the NOS test lab: the real srl_l2 (Layer 2
# Switch) and frr_l3 (Layer 3 Switch) driver packages, plus the two lab nodes
# (nos-lab-srl, container nos-test-srl; nos-lab-frr, container nos-test-frr)
# and enough DUT/
# port/cabling groundwork for a later phase to derive an L2 VLAN membership
# from recorded L1 hops. See docs/NOS_LAB.md.
#
# This runs the full seed_devices_public.py with SEED_NOS=1, so on a fresh
# stack it also lays down the standard demo population (users, devices,
# cabling, topologies). To add ONLY the NOS lab pieces to an already-seeded
# stack, that is fine too: the seed is get-or-create throughout, so existing
# resources are skipped.
#
# Prerequisites:
#   - the HERD stack is up (make up) and reachable at SEED_BASE_URL
#   - the NOS test lab is up (make nos-up) and attached to the stack's Docker
#     network (make nos-attach), so the execution service can reach the lab
#     nodes by container name
#
# Credential resolution mirrors seed_frr_demo.sh: an explicit SEED_EMAIL/
# SEED_PASSWORD wins, else SUPERADMIN_* from .env, else the script default.
# NOS lab SSH creds default to the checked-in lab's fixed values (admin/
# NokiaSrl1! for SR Linux, netadmin/netadmin for FRR); override with
# SEED_NOS_SRL_LOGIN / SEED_NOS_SRL_PASSWORD / SEED_NOS_FRR_LOGIN /
# SEED_NOS_FRR_PASSWORD.
set -euo pipefail

cd "$(dirname "$0")/.."

email=$(grep -E '^SUPERADMIN_EMAIL=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
pw=$(grep -E '^SUPERADMIN_PASSWORD=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)

export SEED_EMAIL="${SEED_EMAIL:-$email}"
export SEED_PASSWORD="${SEED_PASSWORD:-$pw}"
export SEED_BASE_URL="${SEED_BASE_URL:-${HERD_BASE_URL:-https://localhost/api}}"
export SEED_NOS=1

echo "Seeding ${SEED_BASE_URL} with the NOS test lab (SEED_NOS=1) as ${SEED_EMAIL:-<script default>}"
echo "Lab nodes: nos-lab-srl (container nos-test-srl), nos-lab-frr (container nos-test-frr)"

# `--nos-only` (mirrors the seed script's own `--acl-only`) stages just the
# NOS lab pieces against an already seeded stack, skipping the full ~20 min
# default population; pass `--full` to this wrapper to run the whole seed
# (users, DUTs, cabling, topologies, ...) with SEED_NOS=1 layered on top, the
# same way seed_frr_demo.sh always does for SEED_FRR.
if [ "${1:-}" = "--full" ]; then
  uv run python seed_devices_public.py
else
  uv run python seed_devices_public.py --nos-only
fi
