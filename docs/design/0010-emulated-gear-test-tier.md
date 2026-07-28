# Decision: Emulated-Gear Test Tier for Driver Dialects

Status: Proposed. This is a proposal for review, not an accepted decision. Every
decision below is framed as a recommendation with the tradeoffs stated, and the
open questions at the end are the calls that need a human before any of this is
built. Nothing here is final and no code is written yet. It introduces a fourth
driver-testing tier between the deterministic mocks and real hardware, built on the
external `network-simulator` project. Context verified against the live HERD-public
tree and the `network-simulator` working tree on 2026-07-28.

## Context

HERD drives physical and virtual lab gear through admin-uploaded driver packages
(`docs/DRIVERS.md`): a `Driver` class run as a sandboxed subprocess, one of four
connection-type contracts (Layer 1 Switch, Layer 2 Switch, Layer 3 Switch,
Management), each optionally honoring `dry_run`. Driver correctness today is proven
at three fidelity tiers, and there is a gap between the second and the third.

- Tier 1, deterministic mocks. `drivers/mock_l1`, `mock_l2`, `mock_l3`, and
  `mock_hypervisor` implement the package contract and acknowledge port, VLAN,
  route, and instance ops deterministically, with `HERD_mock_fail_actions`,
  `HERD_mock_raise_actions`, and `HERD_mock_sleep_ms` field_data knobs for failure
  and latency injection. They run in CI, drive the live `tests/integration/`
  coverage of provisioning, DLQ retention, and redelivery idempotency, and prove the
  execution service's orchestration is correct. They prove nothing about CLI realism:
  a mock never speaks a vendor dialect, never enters a prompt mode, and never returns
  a vendor's error wording.

- Tier 2, two live FRR routers. `drivers/frr_mgmt` is the one real netmiko driver:
  it SSHes to vtysh and configures an FRRouting router with IOS-style config lines.
  The `network-simulator` slice1 topology (two `frrouting/frr` containers with
  SSH-to-vtysh, plus two Alpine clients) is the real device it drives. This is a
  genuine CLI over a real socket, but it is one NOS (FRR/vtysh), it is exercised
  through the Management contract only, and its live path is manual: `docs/DRIVERS.md`
  and `docs/MANUAL_TESTING.md` case M1 are the FRR live-config check, run by hand
  because CI has no lab. The dry-run half is automated; the on-the-wire half is not.

- Tier 3, real hardware. Physical matrix switches, NOS boxes, and firewalls in a
  real lab. Highest fidelity, lowest availability, not reproducible in CI or in a
  developer loop.

The gap: nothing between tier 2's single FRR dialect and tier 3's physical gear
exercises the CLI dialects, prompt state machines, and error wording of the device
families HERD actually drives. A driver's dialect handling (a MOS `source` directed
cross-connect, a TL1 `ENT-CRS-FIBER` request and its COMPLD/DENY reply, an MRV
`map ... with` symmetric patch, an Arista EOS config-mode transition) is invisible to
tier 1 and absent from tier 2, so a regression in it surfaces only against a live
device. The `network-simulator` project exists to fill this gap: emulated and real-NOS
devices a driver reaches over the network as if they were hardware, so that
"driver-sent equals device-received" is provable without physical gear.

### What network-simulator actually provides today (real versus aspirational)

Grounding this ADR requires being exact about what exists, because the project's
README overstates its own state. Read against the working tree on 2026-07-28:

- Built and on `main`: the L1 recording-emulator CORE. `emulators/l1_matrix/state.py`
  (a directed cross-connect graph with the crosspoint invariants: one source per
  destination, fan-out allowed, plus symmetric `connect_bidir`/`disconnect_bidir`
  wrappers for optical vendors), `grammars/base.py` (a protocol-agnostic Grammar
  interface spanning interactive prompt CLIs and TL1-style request/response), and
  `engine.py` (a transport-free parse-apply-render loop). One vendor grammar,
  `grammars/mos.py` (Arista 7130 / Metamako MOS, directed `source` model), with
  roundtrip unit tests. The slice1 FRR topology, proven end to end (c1 pings c2 across
  two routers, HERD configures them over SSH-to-vtysh).

- NOT on `main`, contrary to the README's "all three vendor grammars are built"
  claim: the MRV and Glimmerglass grammars. They exist only on an unmerged remote
  branch (`origin/feat/l1-mrv-glimmerglass-grammars`). On `main` there is exactly one
  grammar (MOS).

- NOT built at all: the SSH/telnet recording SERVER, the recorder, and the
  introspection API. The README and `base.py` both say so plainly ("the SSH/telnet
  recording server is pending", "a real session ... not yet built"). The engine is
  driven only in-process by unit tests. Consequence that bounds this ADR: the L1
  emulator is not yet reachable over a socket, so a HERD driver cannot connect to it
  today. It is a tested state-plus-grammar library, not yet a network service.

- Reconstructed, not verified: the MOS grammar's error and `show` wording (its
  docstring marks the RECONSTRUCTED strings), and the entire error/latency surface of
  the future recording server. The grammars are verified against the state model in
  CI, not against a real device.

- Deployment: the lab runs on a Proxmox host (`pve`, 192.168.1.23) on an isolated
  `10.99.0.0/24` subnet reached by a static route. It is not reboot-persistent (the
  host NAT rule and the workstation route are noted as not surviving a reboot).

So the honest baseline: the L2/L3 real-NOS path (FRR today, cEOS next) is closer to
reachable than the L1 emulated path, because FRR already answers real SSH and the L1
recording server does not exist yet. The strategic leverage argument still favors L1
(below), but the delivery ordering has to reckon with this transport gap, and it is
called out as a risk and an open question rather than assumed away.

## Decision area 1: device classes and the order to build them (recommendation)

Recommendation: prioritize L1 matrix switches for HERD leverage, but sequence the
actual delivery so the first shippable target rides the path that already works.

Why L1 first for leverage. L1 cross-connect is HERD's most-exercised provisioning
path: every physical reservation wires DUTs through an L1 switch, the fork-save
reconcile (ADR 0006, ADR 0007) is L1 by construction, and the whole connection-driven
apply model (`l1_connection_assignments`, `reservation.wiring_changed`) converges on
L1 hardware. A dialect regression in an L1 driver is therefore the highest-blast-radius
one, and L1 is also where three distinct dialects diverge most (MOS directed `source`,
TL1 request/response, MRV symmetric `map`), so it is where a mock proves the least.
Three grammars are in flight in `network-simulator` (one merged, two on a branch),
which is exactly the L1 dialect coverage this tier wants.

Why the delivery order cannot be a naive "L1, then L2/L3." The L1 emulator has no
transport server yet (Context above), so an L1 emulated driver has nothing to connect
to until `network-simulator` ships the recording SSH/telnet server. The L2/L3 real-NOS
path, by contrast, already answers real SSH via FRR and extends cleanly to cEOS
(a real Arista container, freely licensable with an Arista account, giving a genuine
EOS control plane and CLI). Cisco IOS/IOL, vIOS, CSR images are license-encumbered and
PAN-OS (PA-VM) is heavier still, so those come later.

Recommended build order, smallest-reachable-first, leverage-weighted:

1. Establish the harness against the path that already works (FRR/vtysh over real SSH),
   promoting the manual M1 case into an opt-in automated target. This lands the make
   targets, the env gate, and the seeding without waiting on any new emulator code.
2. cEOS as a real containerlab NOS node, driven by a new EOS Management (and later
   L2/L3) driver. Real dialect, no license blocker, closes the "second real NOS" gap.
3. L1 emulated dialects (MOS first, then MRV and Glimmerglass) once
   `network-simulator` ships the recording server. Highest leverage, but gated on the
   upstream transport work, so it is third by readiness even though it is first by
   value.
4. Cisco IOS and PAN-OS later, license and image weight permitting.

The device-class priority is the first open question for review: confirm L1-by-leverage
versus a strict readiness order that would put cEOS first.

## Decision area 2: one HERD driver per dialect, each an ordinary package (recommendation)

Recommendation: model every emulated or real-NOS device as a normal HERD device with
a normal admin-uploaded driver package, one package per CLI dialect, each following the
`frr_mgmt` pattern and each honoring `dry_run`. The emulator or NOS endpoint is
registered in HERD inventory as a real device with real credentials (host, login,
password via the `HERD_`-prefixed field_data keys), indistinguishable from hardware to
the execution service.

This is the load-bearing design choice, and it falls straight out of the existing
contract: the execution service already treats a driver as opaque code that opens a
socket to `HERD_ip` and speaks a protocol. It cannot tell an emulator from a real box,
and it must not need to. Concretely:

- L1 dialects map to the Layer 1 Switch contract (`login`, `logout`, `connect_ports`,
  `disconnect_ports`, `status`). One driver per dialect: a MOS driver, a TL1
  (Glimmerglass) driver, an MRV driver. Each translates the neutral
  `connect_ports(port_a, port_b)` / `disconnect_ports(port_a, port_b)` calls into its
  vendor syntax (MOS `interface etN` then `source etX` for the directed edge; MRV
  `map <a> with <b>` symmetric; Glimmerglass `ENT-CRS-FIBER` / `DLT-CRS-FIBER` TL1
  with a ctag), over SSH or telnet as the dialect dictates. The directed-versus-
  symmetric distinction the emulator's state model draws (MOS directed, MRV and
  Glimmerglass symmetric) is a driver-side concern the driver already has to get right
  against real gear, and this tier is exactly where it gets tested.

- L2/L3 and Management dialects map to their contracts. EOS over SSH is a Management
  driver first (`login`, `logout`, `configure`, `backup`, `status`), the same shape as
  `frr_mgmt`, and can grow Layer 2 / Layer 3 Switch variants
  (`create_vlan`/`add_to_vlan`, `configure_route`/`remove_route`) as the cEOS node
  gains those roles.

- Published config schemas ride along unchanged (ADR 0002). `frr_mgmt` already
  publishes a `config_schema()` for its raw-vtysh vocabulary; an EOS or dialect driver
  that needs a vocabulary the registry's `additionalProperties: false` schema would
  reject publishes its own the same way. Nothing new in the driver-loading path.

The alternative, a single parameterized "emulator driver" that branches on dialect
internally, is rejected: it would hide the per-dialect translation this tier exists to
test behind a conditional, and it would not resemble how a real per-vendor driver is
written. One package per dialect keeps each driver honest and uploadable exactly as a
production driver would be.

## Decision area 3: the test tier, modeled on the LDAP opt-in pattern (recommendation)

Recommendation: add an opt-in Makefile target family for the emulated-gear tier,
env-gated on lab reachability, following the established `make ldap-up` /
`make test-auth-ldap` / `make ldap-down` pattern for an external-dependency suite.
That pattern is the precedent for a test tier that needs a resource outside the repo
and outside GitHub-hosted CI: the LDAP targets start an external container living at
`HERD_LDAP_DIR`, fail with a clear message when it is absent, and gate a live suite
(`tests/test_ldap_service_live.py`) that PR CI never runs.

Proposed targets (names for review):

- `make emulator-up`: bring up the `network-simulator` lab (the containerlab slice and,
  once it exists, the L1 recording server), or verify reachability of an already-running
  lab on the Proxmox host. Fails closed with a pointer to `network-simulator` setup when
  the lab dir or host is not configured, exactly as `ldap-up` does.
- `make test-drivers-emulated`: run the driver dialect suite against the reachable lab.
  Gated on an env flag plus a reachability probe of the lab subnet, so a bare
  `make test` never touches it.
- `make emulator-down`: stop or release the lab.

Where it runs:

- Locally, in a developer loop, when the lab is reachable (the static route to
  `10.99.0.0/24` is present).
- In the self-hosted nightly context (`nightly.yml` already runs the heavy suites that
  PR CI skips), if and only if the nightly runner can reach the Proxmox lab. This is an
  open question (below): the lab is currently a single non-reboot-persistent host, so
  wiring it into nightly needs a decision on where the emulator processes run and how
  the runner reaches them.
- NOT in GitHub-hosted PR CI. NOS images are license-gated (cEOS needs an Arista
  account; Cisco and PAN-OS images are encumbered), and the lab is external, so the
  GitHub-hosted `backend`/`frontend`/`integration` jobs must stay independent of it,
  exactly as they are independent of LDAP. Whether the L1 RECORDING emulators (which are
  pure Python, no licensed image) could later be packaged to run inside a
  GitHub-hosted job is a genuine possibility and is flagged as an open question, not
  assumed.

Failure-injection story, stated honestly. This tier's fidelity is higher than the
mocks on dialect and lower than the mocks on fault control, and the two are
complementary rather than a replacement:

- What the L1 recording emulators can replay: the real request/response shape and,
  where a real transcript has been captured, the exact success and error wording of a
  dialect. What they CANNOT yet replay: any error path or wording that has not been
  recorded (the MOS grammar's error and `show` strings are RECONSTRUCTED, not verified,
  and the recording server that would capture real transcripts does not exist yet), and
  arbitrary latency or transient-failure injection. The recording model's fidelity
  ceiling is the set of transcripts it has seen; an unrecorded error is a coverage gap,
  not a faithful failure.
- What the mock drivers still own: deterministic, parameterized failure and latency
  injection (`HERD_mock_fail_actions`, `HERD_mock_raise_actions`, `HERD_mock_sleep_ms`)
  that drive the execution service's retry, DLQ, redelivery, and ack-heartbeat paths.
  The emulated tier does not replace this; a real emulator cannot be told "fail the
  third connect_ports with a 503" the way a mock can.
- What the real NOS nodes (FRR, cEOS) add over both: a genuine control plane whose
  errors are real, not reconstructed, at the cost of no injection knob (you get the
  error the NOS actually produces for the input you send).

So the tier's job is dialect and prompt-state-machine correctness, not fault-path
coverage. The mocks keep the fault-path coverage. Both stay.

## Decision area 4: seeding and topology (recommendation)

Recommendation: extend the existing seed-script family to register emulated devices and
their cabling, following the `scripts/seed_frr_demo.sh` pattern. That script already
seeds the two slice1 FRR routers as HERD devices (`frr-r1` 10.99.0.11, `frr-r2`
10.99.0.12) with the netmiko `frr_mgmt` driver via `SEED_FRR=1`, resolving credentials
from `SEED_FRR_LOGIN`/`SEED_FRR_PASSWORD` (defaulting to the lab values), and it is
idempotent (get-or-create throughout). It is the exact template.

Proposed: a companion seed path (an env flag such as `SEED_EMULATED=1`, or a sibling
`scripts/seed_emulated_lab.sh`) that uploads each dialect driver package and registers
the emulator endpoints as devices with their `HERD_ip`/`HERD_login`/`HERD_password`
field_data plus the L1/L2/L3 cross-connect cabling the dialect suite exercises. It stays
idempotent and get-or-create, so it layers onto an already-seeded stack the way
`seed_frr_demo.sh` does. A new repo-root seed script must be added to the Makefile
`ROOT_PY` lint list and the CI lint lines, per the repo convention.

## Phased delivery

Smallest-first, each phase independently valuable and independently mergeable, matching
the delivery discipline of ADR 0007 and the #32/P3a pattern.

1. Harness on the working path. Promote the manual M1 FRR live-config case into an
   opt-in automated target: `make emulator-up` / `test-drivers-emulated` /
   `emulator-down`, env-gated and reachability-probed like the LDAP targets, running the
   existing `frr_mgmt` live path against the slice1 lab. This lands the tier's plumbing
   with zero new emulator or driver code and immediately retires a manual checklist item.
2. cEOS real NOS. Stand up a cEOS containerlab node in `network-simulator`, author an
   EOS Management driver on the `frr_mgmt` pattern, and add its dialect tests to the
   suite. First second-NOS dialect under real SSH.
3. L1 emulated dialects, gated on the upstream recording server. Once `network-simulator`
   ships the SSH/telnet recording server and merges the MRV/Glimmerglass grammars, author
   the MOS, MRV, and TL1 L1 Switch drivers and their `connect_ports`/`disconnect_ports`
   dialect tests. Highest leverage; delivered when the transport exists.
4. Seeding and breadth. The `SEED_EMULATED` path (Decision area 4), plus EOS L2/L3
   variants and, license permitting, Cisco IOS and PAN-OS nodes and drivers.
5. Docs. `docs/DRIVERS.md` gains a per-dialect note, `docs/MANUAL_TESTING.md` M1 flips
   from manual to automated-opt-in, and FEATURES/PLANNED_FEATURES reflect the new tier
   (the "Hardware-in-the-loop" future-considerations line is the closest existing hook).

## Risks

1. Emulator fidelity drift. The recording emulators are faithful only to the transcripts
   they have captured, and several dialect strings are currently RECONSTRUCTED, not
   verified (the MOS error and `show` wording; the entire Glimmerglass create/delete
   path on the unmerged branch). A driver test that passes against a reconstructed string
   proves consistency with a guess, not with a real device, until a real transcript
   corrects it. Mitigation: mark reconstructed expectations as provisional in the test
   suite the way the grammars mark them in code, and treat a captured-transcript
   correction as the promotion from provisional to verified.
2. Recording coverage gaps. Error and edge-case paths that have not been recorded are
   simply absent; the tier cannot exercise a failure it has never seen. The mock drivers
   remain the fault-path coverage (Decision area 3), and this tier must not be sold as
   replacing them.
3. Two repos in lockstep. HERD and `network-simulator` are separate projects with
   separate CI and branch protection. A dialect driver in HERD and its emulator grammar
   in `network-simulator` must evolve together, and a change on one side can silently
   break the other's suite because neither repo's PR CI runs the cross-repo tier.
   Mitigation: pin the tier to the nightly context and document the cross-repo
   dependency; a version or commit pin between the two is an open question.
4. Licensing. cEOS needs an Arista account; Cisco IOS/IOL/vIOS/CSR and PAN-OS/PA-VM
   images are license-encumbered. This is the reason the tier is nightly/self-hosted and
   not GitHub-hosted, and it caps which NOS families can ever run in a hosted job.
5. Lab availability and persistence. The lab is a single Proxmox host on a non-reboot-
   persistent subnet (the NAT rule and the workstation route do not survive a reboot),
   and the L1 recording server does not exist yet. The tier's reachability gate must fail
   closed and clearly (the LDAP pattern), and nightly integration depends on making the
   lab durable and reachable from the runner.
6. Upstream dependency ordering. The highest-leverage device class (L1) is gated on
   `network-simulator` work not yet started (the recording server) and work not yet
   merged (two of three grammars). If that upstream slips, phase 3 slips; phases 1 and 2
   are deliberately independent of it so the tier delivers value regardless.

## Open questions for review

1. Device-class priority. Confirm L1-by-leverage as the strategic priority, or prefer a
   strict readiness order that ships cEOS (real, reachable, unblocked) before the L1
   emulated dialects (higher leverage, blocked on the upstream recording server)?
   Decision area 1 recommends leverage-priority with readiness-ordered delivery; this is
   the call to ratify.
2. Where the emulator processes run for nightly. The lab is one non-reboot-persistent
   Proxmox host today. For the tier to run in the self-hosted nightly context, where do
   the emulator and NOS processes live, and how does the nightly runner reach them
   (persistent lab subnet, a runner on the lab network, a tunnel)? This gates phase 1's
   nightly integration.
3. Could the L1 recording emulators eventually run inside GitHub-hosted CI? They are
   pure Python with no licensed image, unlike the NOS nodes. If packaged as a service the
   suite could spawn, the L1 dialect tests (the highest-leverage ones) might run on every
   PR, not only nightly. Worth scoping, or deliberately out of scope?
4. Issue tracking ownership. Do the emulator-side work items (the recording server, the
   MRV/Glimmerglass grammar merge, cEOS topology) belong in this repo's issue tracker,
   in `network-simulator`'s, or split by which repo the change lands in? The two-repos-
   in-lockstep risk makes this a process question worth settling before work starts.
5. Cross-repo version pinning. Should HERD's emulated tier pin a specific
   `network-simulator` commit or tag, so a green nightly is reproducible, or track its
   `main`? Related to risk 3.
