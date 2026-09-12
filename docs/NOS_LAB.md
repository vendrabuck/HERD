# Emulated-gear test lab (phase 0)

This is a checked-in, license-free lab of two real network operating systems,
run as Docker containers on the local host: no external Proxmox host, no
paid license, no separate lab reservation. It is phase 0 of the
emulated-gear test tier sketched in `docs/design/0010-emulated-gear-test-tier.md`
(status Proposed): the tier between HERD's deterministic mock drivers
(`drivers/mock_l1`, `mock_l2`, `mock_l3`, `mock_hypervisor`) and real hardware
in a physical lab.

## Why it exists

The mock drivers prove HERD's orchestration logic is correct: retries, DLQ
handling, redelivery idempotency, the reservation state machine. They prove
nothing about CLI realism, because a mock never speaks a vendor dialect,
never enters a real prompt mode, and never returns a vendor's actual error
wording. A driver's dialect handling is invisible to the mocks and, until
now, exercised only by hand against `drivers/frr_mgmt`'s one FRRouting
target (`docs/MANUAL_TESTING.md` case M1) or against real hardware.

This lab gives every developer and CI (once wired in; it is opt-in for now,
see below) two real, differently-shaped network operating systems to drive:
one Cisco-dialect Layer 3 router and one Nokia-dialect Layer 2 switch. It
exists apart from the external `network-simulator` project referenced in
ADR 0010: that project needs its own Proxmox host and is not reboot-persistent,
while this lab is two `docker compose` services that boot from a public,
free image (SR Linux) and a Dockerfile built on a free base image (FRR), and
reseed themselves on every `up`.

## The two nodes

| Node | Image | Role | netmiko `device_type` | Port | Credentials |
|---|---|---|---|---|---|
| `srl` | `ghcr.io/nokia/srlinux:26.7.2-519` (pinned by digest; see the compose file's comment) | Layer 2 target (mac-vrf / bridged subinterfaces) | `nokia_srl` | `${HERD_TEST_SRL_PORT:-2223}` (SSH) | `admin` / `NokiaSrl1!` |
| `frr` | built from `infra/nos-test/frr/Dockerfile` (`frrouting/frr` base, pinned by digest with no matching version tag; see the Dockerfile's comment) | Layer 3 / Cisco-dialect target (vtysh, IOS-style syntax) | `cisco_ios` | `${HERD_TEST_FRR_PORT:-2224}` (SSH) | `netadmin` / `netadmin` |

Both nodes run `privileged: true`: SR Linux manages its own network
namespaces and interfaces at boot, and FRR's zebra/staticd daemons program
the kernel routing table and need raw-socket access.

The lab is deliberately stateless, the same design `infra/ldap-test/` uses:
neither service has a named volume, so `docker compose down` (via
`make nos-down`) discards all node state, and the next `up` (via
`make nos-up`) boots both nodes from a clean image and reapplies the
checked-in baseline. A broken node is fixed with `make nos-reset`, never by
hand-repairing a stateful container.

## Make targets

- `make nos-up`, start both nodes and wait until healthy (builds the FRR
  image first if needed)
- `make nos-down`, stop and remove both nodes
- `make nos-status`, show container status
- `make nos-logs`, tail both nodes' logs
- `make nos-reset`, tear down and rebuild from scratch
- `make nos-attach`, attach both lab containers to the DEV stack's Docker
  network, so HERD's own execution service can reach them by CONTAINER NAME
  (`nos-test-srl`, `nos-test-frr`) over Docker DNS (phase 3a, ADR 0010)
- `make nos-detach`, detach both lab containers from the dev stack's network
- `make nos-test-dialect`, run the four dialect suites, booting the lab first
  if it is not already up (see "Where these run" below)
- `make nos-test-feature`, run the two via-stack suites against a running
  stack: attach, seed, run, always detach

None of these run as part of `make test`, `make master`, or `make everything`;
locally this phase is opt-in only, so a host that never runs it pays nothing
for it. The two test targets DO run in GitHub Actions, on different
schedules; see "Where these run" below.

## Phase 3a: wiring the lab into a running HERD stack

Phases 0 to 2 (above) drive the lab nodes directly, over the host-published
SSH ports, bypassing HERD's own execution service entirely. Phase 3a wires
the lab INTO a running dev stack so HERD drives the real devices through its
own API, execution service, and driver sandbox, the same way it drives any
other device.

The mechanism is `docker network connect`: once a lab container is
attached to the dev stack's Docker network (`herd-public_herd-net` by
default; a differently-named checkout gets a differently-named network, see
`make nos-attach`'s own project-name resolution), the execution service's
container reaches it by CONTAINER NAME over Docker DNS, not by the host-
published port. Container IPs are not stable across a `docker compose`
recreate; container names are, which is why devices registered this way
carry the container name in `field_data.ip`, not an IP address.

Workflow:

```bash
make up                    # dev stack up
make nos-up                # NOS test lab up (if not already)
make nos-attach             # wire the lab into the dev stack's network
make seed-nos              # register the real drivers, the two lab nodes,
                          # and DUT/port/cabling groundwork
```

`make seed-nos` runs `python -m seedtools nos`, which stages just the NOS lab
pieces and skips the ~20 min default seed population; `python -m seedtools nos
--full` runs the whole seed with the NOS lab layered on, mirroring
`python -m seedtools frr --full` for the FRR demo (`make seed` with `SEED_NOS=1`
in the environment does the same thing). It is get-or-create throughout: registers the real
`drivers/srl_l2` (Layer 2 Switch) and `drivers/frr_l3` (Layer 3 Switch)
driver packages, a `NOS Lab SR Linux L2 Switch` and `NOS Lab FRR L3 Switch`
device template each, the two lab devices (`nos-lab-srl`, container
`nos-test-srl`; `nos-lab-frr`, container `nos-test-frr`; with the credentials from the table
above), two placeholder DUT devices, and cables both DUTs to the SR Linux
node's `ethernet-1/1` and `ethernet-1/2` ports, the two interfaces the
checked-in baseline already enables and VLAN-tags. This is inventory-side
groundwork only (devices, ports, cabling); it deliberately creates no
topology or reservation, since ADR 0009's L2 membership and ADR 0014's L3
routing intent are both driven through a reservation fork, not a bare
device registration. See `seedtools/nos_lab.py`'s `seed_nos_lab` for the
exact shape.

`make nos-detach` reverses `make nos-attach`. A `docker compose down` (or
`make down`) while a lab container is still attached prints "Resource is
still in use" for the stack's network but still exits 0 (verified live), so
a forgotten detach never breaks `make down`/`make clean`; it DOES leave the
stack's network behind after the stack itself is gone, so always pair
`make nos-attach` with an eventual `make nos-detach`.

## Phase 3a proof: driving a real device through HERD's own API

`tests/nos_lab/test_frr_l3_via_stack_live.py` is the end-to-end proof that
phase 3a is real: it drives the checked-in FRR node through HERD's normal
Layer 3 Switch path (a reservation whose topology fork carries
`data.l3.routes` routing intent, ADR 0009/0014), not by calling the driver
directly. It creates its own throwaway driver/template/device/topology/
reservation (independent of `seed_nos_lab`, so it needs no prior seed run),
then:

- applies a real static route at ACTIVATION (create_fork writes the canvas
  intent tolerantly and by design does not gate) and verifies BOTH the
  execution run's own SUCCESS status and, independently,
  `docker exec nos-test-frr vtysh -c "show ip route static"`;
- then runs a fork SAVE carrying a CHANGED route set, which is the only path
  that exercises the gated save (`gate_l3_intent`), the staged
  `reservation.wiring_changed`, and execution's stay-adjacent route-set
  reconcile (`removes = pinned - intent`, `adds = intent - pinned`, removes
  driven before adds inside one login/logout, ADR 0014 Decision 3). Both
  halves are verified on the router itself: the old prefix is gone and the
  new one is installed;
- removes the route by cancelling the reservation (ADR 0014's deprovision
  reconcile) and independently verifies it is gone;
- proves the flip side of the contract: a route the real device REJECTS
  (an interface name with an embedded second token vtysh cannot parse,
  verified live to reproduce a genuine `% Unknown command` rejection)
  records a FAILED execution run carrying the device's own wording (the
  assertion pins the `% Unknown command:` prefix plus the destination and
  the offending interface, not merely a non-empty string), and
  independently verifies nothing was installed.

Every destination assertion matches the FULL prefix FRR prints
(`192.0.2.4/30`), never the bare network address, which would also match a
leaked neighbouring prefix such as `192.0.2.40/30`. A syntactically malformed
  destination or next-hop (bad octets, the classic example) cannot reach
  the device this way: cabling's own save-time L3 intent gate validates
  every route with `ipaddress.ip_network()`/`ip_address()` before accepting
  the fork save, so that case is refused upstream (422
  `l3_intent_malformed`) and never reaches execution or the driver.

Needs BOTH the lab (`make nos-up`) and a running dev stack with the lab
attached (`make up`, `make nos-attach`); it lives under `tests/nos_lab/`
(never invoked by `make test`, `make master`, or `make everything`) rather
than `tests/integration/` (which IS invoked by those, against an ephemeral
gate stack the lab is never attached to). Same gating convention as the
other lab-live tests: skips automatically when either precondition is
missing, and `HERD_TEST_NOS_REQUIRED=1` turns a missing precondition into a
hard failure. Run it with:

```bash
make up
make nos-up
make nos-attach
make seed-nos

# The test authenticates as the stack's superadmin and reads those credentials
# from the ENVIRONMENT, while the stack seeds them from .env, so export them
# first. Skipping this step is the one easy way to get a bare 401; the suite
# probes for it up front and says so rather than failing inside a test.
export SUPERADMIN_EMAIL=$(grep -E '^SUPERADMIN_EMAIL=' .env | head -1 | cut -d= -f2-)
export SUPERADMIN_PASSWORD=$(grep -E '^SUPERADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)

HERD_TEST_NOS_REQUIRED=1 uv run pytest tests/nos_lab/test_frr_l3_via_stack_live.py -v
make nos-detach
```

## Phase 3b proof: a real Layer 2 VLAN membership through HERD's own API

`tests/nos_lab/test_srl_l2_via_stack_live.py` is the Layer 2 counterpart of the
phase 3a proof above: it proves HERD derives a Layer 2 VLAN membership from a
reservation's wiring (ADR 0009: membership is derived from RECORDED HOPS, not
per-hop deltas, and always full-reconciles on a `reservation.wiring_changed`
event) and configures it on the real SR Linux node through HERD's execution
service and driver sandbox, not by calling `drivers/srl_l2` directly.

Unlike the phase 3a test, it reuses the SEEDED `nos-lab-dut-1`,
`nos-lab-dut-2`, and `nos-lab-srl` devices and their cabling (run
`make seed-nos` first) rather than creating throwaway devices:
membership derivation depends on cabling's pathfinder walking a real physical
connections graph, which only exists between the seeded lab devices. It activates the
reservation over an EDGELESS canvas and only then saves a fork that ADDS the
DUT-to-DUT edge (no switch node; the pathfinder resolves it through the real
switch's `ethernet-1/1`/`ethernet-1/2` ports). That ordering is what makes the
save's effect observable: with nothing wired at activation, and the applied
fork version polled to prove activation's own reconcile ran and derived
nothing, every subinterface binding that appears afterwards is attributable to
the save's connection-driven reconcile. It reads the VLAN id HERD allocated
from `GET /reservations/{id}/wiring-status` instead of assuming a number,
independently verifies on the real device that the `mac-vrf` network-instance
exists AND both subinterfaces are bound into it, cancels the reservation, and
independently verifies both the bindings and the VLAN definition are gone,
cross-checking HERD's own ledger (RELEASED) against the device at each step.

The device baseline is snapshotted BEFORE the reservation exists, so nothing
HERD does can race it, and it is scoped to what this test actually uses: the
two ports carry no subinterface, and the VLAN HERD later allocated was absent
from the pre-reservation `show network-instance summary`. An unrelated
`mac-vrf` left on the switch by another lane therefore no longer fails this
test with a misleading message. The teardown that removes any leftover
membership from the device checks its own exit status and fails with the
`sr_cli` stderr, so a silently failed cleanup cannot resurface as the next
run's baseline failure.

Same gating, credential-export, and cleanup discipline as the phase 3a test.
In both files the preconditions are probed from a session-scoped fixture
rather than at import, so plain collection (the repo-root `testpaths` means a
bare `uv run pytest` collects them) opens no socket, runs no `docker`, and
logs in nowhere. Run it with:

```bash
make up
make nos-up
make nos-attach
make seed-nos
export SUPERADMIN_EMAIL=$(grep -E '^SUPERADMIN_EMAIL=' .env | head -1 | cut -d= -f2-)
export SUPERADMIN_PASSWORD=$(grep -E '^SUPERADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)

HERD_TEST_NOS_REQUIRED=1 uv run pytest tests/nos_lab/test_srl_l2_via_stack_live.py -v
make nos-detach
```

## Tests

- `tests/unit/test_nos_lab_compose.py`, static, runs in CI with no lab: pins
  the compose project name, the stateless (no-named-volumes) property, that
  both services declare a healthcheck, that the published ports do not
  collide with the dev or gate compose files' ports, and the shape of the
  checked-in SR Linux baseline file.
- `tests/unit/test_seed_nos_lab_driver.py`, static, runs in CI with no lab
  or stack: pins that `seedtools.nos_lab`'s `seed_nos_lab` zips the
  real `drivers/srl_l2` and `drivers/frr_l3` packages from disk (never
  drifting from the source of truth) and degrades gracefully when either
  package is missing, mirroring `tests/unit/test_seed_frr_driver.py`.
- `tests/nos_lab/test_nos_lab_live.py`, opt-in, needs the lab running.
  Skips automatically when the lab is not reachable. Set
  `HERD_TEST_NOS_REQUIRED=1` to turn an unreachable lab into a hard failure
  instead of a skip (the same convention
  `services/auth/tests/test_ldap_service_live.py` uses with
  `HERD_TEST_LDAP_REQUIRED`), so a CI job that asks for these tests
  explicitly never silently no-ops. Run it with:

  ```bash
  make nos-up
  HERD_TEST_NOS_REQUIRED=1 uv run pytest tests/nos_lab/test_nos_lab_live.py tests/nos_lab/test_frr_l3_driver_live.py tests/nos_lab/test_frr_mgmt_driver_live.py tests/nos_lab/test_srl_l2_driver_live.py -v
  ```

  It creates a VLAN (SR Linux) and a static route (FRR) with a random,
  per-run-unique identifier, verifies each change independently of the
  netmiko session that made it (a separate `docker exec` into the
  container, never the driver's own status method), and cleans up
  afterward so the lab stays re-runnable without a reset.

  The other two files in this directory, `test_frr_l3_via_stack_live.py` and
  `test_srl_l2_via_stack_live.py`, additionally need a running dev stack with
  the lab attached and seeded (`make up`, `make nos-attach`,
  `make seed-nos`); see the phase 3a and phase 3b sections above,
  which already document them.
- `tests/nos_lab/test_frr_l3_driver_live.py`, opt-in, same gating as above,
  drives the checked-in `drivers/frr_l3` Layer 3 Switch reference driver
  (not raw netmiko) against the FRR node, verifying every route change
  independently via `docker exec ... vtysh` and covering both route forms
  plus the driver's idempotency behavior; see docs/DRIVERS.md's "FRR
  reference driver" section.
- `tests/nos_lab/test_frr_mgmt_driver_live.py`, opt-in, same gating as
  above, drives the checked-in `drivers/frr_mgmt` Management reference
  driver (added by #773) against the FRR node: proves a vtysh rejection is
  classified and reported as `success: False` rather than a clean apply,
  that an already-absent removal reports success without masking a genuine
  failure elsewhere in the same batch, and the failed-save case (the
  startup config fails to persist); see docs/DRIVERS.md's "A driver must
  report a device rejection as a failure" section.
- `tests/nos_lab/test_srl_l2_driver_live.py`, the same opt-in gating,
  scoped to the SR Linux node only. Exercises the real
  `drivers/srl_l2/driver.py` (Nokia SR Linux Layer 2 Switch driver, see
  `docs/DRIVERS.md`) end to end: create a VLAN, add a port to it tagged and
  untagged, verify the mac-vrf and the port binding independently via
  `sr_cli` (never through the driver's own session), prove `create_vlan`
  idempotency and `delete_vlan`-on-a-missing-VLAN, then remove and delete
  and verify both are gone.

## Where these run

The suites are split into two tiers (issue #785, decided 2026-09-12), one
Makefile variable each, so a workflow and a local run share one recipe and
cannot drift:

- Dialect tier, `make nos-test-dialect` (`NOS_DIALECT_TESTS`): the four
  suites that drive one driver against one lab node over SSH, with no HERD
  stack involved. Runs on every pull request from the `nos-dialect` job in
  `.github/workflows/ci.yml`, which boots the lab, runs the suites
  hard-required (`HERD_TEST_NOS_REQUIRED=1`), uploads the lab logs as the
  `nos-dialect-logs` artifact if anything fails, and stops the lab again.
  The job is advisory: the branch-protection ruleset requires only `backend`
  and `frontend`, so a red run reports without blocking a merge. Run locally
  with `make nos-test-dialect`, which boots the lab if it is not already up
  and tears down only what it started.
- Feature tier, `make nos-test-feature` (`NOS_FEATURE_TESTS`): the two
  via-stack suites that drive a real device through HERD's own API. Runs in
  `.github/workflows/nightly.yml`, step "NOS lab feature tests
  (reservation-driven, real SR Linux and FRR)", placed after the seeded e2e
  pass (the L2 suite reuses the `nos-lab-*` devices `make seed-nos` creates)
  and before the locust load test (both hold reservations over the same
  devices, so running them together would race for ports). Locally it needs
  a running stack and a running lab: `make up`, `make nos-up`, then
  `make nos-test-feature`. It attaches the lab, runs `make seed-nos`, runs
  the suites, and always detaches. Export `SUPERADMIN_EMAIL` and
  `SUPERADMIN_PASSWORD` (or `SEED_EMAIL` / `SEED_PASSWORD`) first; the
  suites read them from the environment, not from `.env`.

Local gates (decided 2026-09-12): `make everything` runs the dialect tier as
a phase of its own recipe, after the live LDAP auth phase and before frontend
coverage; that phase boots the lab if it is not already up and tears down
only what it started, the same as a standalone `make nos-test-dialect` call.
`make master` does not run it, and stays untouched. The feature tier is
nightly-only either way: neither local gate runs `make nos-test-feature`,
since it needs a running stack plus the lab together, a heavier local ask
than the dialect tier carries.

`tests/unit/test_nos_lab_ci_wiring.py` pins all of this statically, including
that every file under `tests/nos_lab/` appears in exactly one of the two
lists, so a new suite cannot be merged with nothing running it.

## The `[FACTORY]` trap and why the baseline exists

A factory-fresh SR Linux node's candidate-mode config prompt reads:

```
--{ [FACTORY] + candidate private private-admin }--
```

netmiko 4.7.0's `nokia_srl.check_config_mode` regex expects the mode marker
immediately after `--{` (one of a fixed set of space/asterisk/plus
combinations); the `[FACTORY]` token breaks that match, so
`send_config_set` fails with `Failed to enter configuration mode.` before a
single command is ever sent.

Running `save startup` once clears the `[FACTORY]` tag; after that, netmiko
works unchanged against the same node. So the lab applies and saves a small
CLI baseline (`infra/nos-test/srl/baseline.cli`, mounted read-only and piped
into `sr_cli` by `infra/nos-test/srl/start.sh` on every boot) that enables
and VLAN-tags the two interfaces the live tests use, commits, and saves. The
baseline is intentionally small and reviewable, unlike a full saved
`config.json` (over 100 KB), and reapplying it against an already-configured
node is harmless: SR Linux commits a no-op diff cleanly.

The healthcheck proves the baseline actually landed, not merely that
`sr_cli` answers: it checks that `/etc/opt/srlinux/config.json` (the saved
startup config) exists and that the LAST setting the baseline applies
(`ethernet-1/2 vlan-tagging`) reads back `true`. A healthy container
therefore implies the whole baseline ran to completion.

## The FRR node's VRF fixture

`infra/nos-test/frr/start.sh` creates a Linux VRF at boot, before sshd, so the
FRR node carries a deterministic virtual router the way the SR Linux node
carries its baseline (ADR 0014 addendum X-G, issue #755):

| Object | Value |
|---|---|
| VRF device | `blue`, routing table `10` |
| Member interface | `dummy0` (a dummy device enslaved to `blue`) |
| Member address | `192.0.2.254/30` (RFC5737 TEST-NET-1) |

Why it has to exist at boot: FRR accepts `ip route <prefix> <next_hop> vrf
<name>` into its configuration whether or not a Linux VRF device with that name
exists, but with no device it never installs the route. vtysh answers
`Static Route to <prefix> not installed currently because dependent config not
fully available` (a line with no `%` marker, which is why `drivers/frr_l3`
classifies it separately; see docs/DRIVERS.md), and `show ip route vrf <name>`
answers `% VRF <name> not active`. Without the fixture the lab could only ever
prove the failure case.

Creation is idempotent (each step is skipped when the object already exists) so
a container restart re-enters it cleanly, and nothing ever deletes it: FRR
refuses `no vrf <name>` with `% Only inactive VRFs can be deleted` while the
Linux device exists. Tests treat the fixture as permanent and clean up only
their own routes.

Verifying a VRF route independently, the way the live tests do (never through
the driver's own session):

```
docker exec nos-test-frr ip route show table 10
docker exec nos-test-frr vtysh -c "show ip route vrf blue static"
```

`tests/unit/test_nos_lab_compose.py` carries a static pin that start.sh still
mentions `type vrf` and `dummy0`, so a lab-less CI run catches an edit that
drops the fixture.

## SR Linux versus Cisco Layer 2

SR Linux's Layer 2 model is not Cisco-shaped, and a driver written against
one will not work against the other unchanged:

- Cisco IOS models a VLAN as a single global object (`vlan 100`) that a
  trunk or access port references directly.
- SR Linux has no bare "VLAN" object. A bridged domain is a
  `network-instance <name> type mac-vrf`; a physical interface must first
  have `vlan-tagging true` set, then a `subinterface <N> type bridged` is
  created on it with `vlan encap single-tagged vlan-id <N>`, and finally
  that subinterface (`ethernet-1/1.<N>`) is bound into the mac-vrf with
  `network-instance <name> interface ethernet-1/1.<N>`. All of this happens
  inside candidate mode, followed by `commit stay`.

This is exactly the kind of dialect difference tier 1 (deterministic mocks)
cannot expose and tier 2 (the single FRR target) never touches, since FRR's
own Layer 2 story is out of scope for the `frr_mgmt` driver. The live SR
Linux test in this lab exercises it directly.
