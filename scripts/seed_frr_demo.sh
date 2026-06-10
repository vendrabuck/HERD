#!/usr/bin/env bash
# Seed a running HERD stack with the FRR live-config demo: the real netmiko FRR
# Management driver plus the two slice1 lab routers (frr-r1 10.99.0.11,
# frr-r2 10.99.0.12). Idempotent; safe to re-run.
#
# This runs the full seed_devices_public.py with SEED_FRR=1, so on a fresh stack it
# also lays down the standard demo population (users, devices, cabling, topologies).
# To add ONLY the FRR pieces to an already-seeded stack, that is fine too: the seed
# is get-or-create throughout, so existing resources are skipped.
#
# Prerequisites:
#   - the HERD stack is up (make up) and reachable at SEED_BASE_URL
#   - for live config (not just dry-run), the network-simulator slice1 lab is deployed
#     on the Proxmox host and 10.99.0.0/24 is routable from the execution service
#
# Credential resolution mirrors the Makefile's _everything-seed target: an explicit
# SEED_EMAIL/SEED_PASSWORD wins, else SUPERADMIN_* from .env, else the script default.
# FRR SSH creds default to netadmin/demo123 (the slice1 lab values); override with
# SEED_FRR_LOGIN / SEED_FRR_PASSWORD.
set -euo pipefail

cd "$(dirname "$0")/.."

email=$(grep -E '^SUPERADMIN_EMAIL=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
pw=$(grep -E '^SUPERADMIN_PASSWORD=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)

export SEED_EMAIL="${SEED_EMAIL:-$email}"
export SEED_PASSWORD="${SEED_PASSWORD:-$pw}"
export SEED_BASE_URL="${SEED_BASE_URL:-${HERD_BASE_URL:-https://localhost/api}}"
export SEED_FRR=1

echo "Seeding ${SEED_BASE_URL} with the FRR live-config demo (SEED_FRR=1) as ${SEED_EMAIL:-<script default>}"
echo "FRR routers: frr-r1 (10.99.0.11), frr-r2 (10.99.0.12), login ${SEED_FRR_LOGIN:-netadmin}"
uv run python seed_devices_public.py
