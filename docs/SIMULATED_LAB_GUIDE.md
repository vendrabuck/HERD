# Simulated lab guide

HERD ships three different kinds of simulated hardware, each built for a different
job. This guide is task-oriented: it tells you which one to reach for, how to boot
it, and how to drive a real device through HERD's own UI. For the build history and
implementation detail behind the NOS lab, see `docs/NOS_LAB.md`; for driver package
internals, see `docs/DRIVERS.md`.

## 1. The three tiers

| Tier | What it is | Layers and actions | Real CLI or protocol | When to reach for it | Cost to run |
|---|---|---|---|---|---|
| 1. Mock drivers | Four hardware-free Python driver packages under `drivers/mock_l1/`, `drivers/mock_l2/`, `drivers/mock_l3/`, `drivers/mock_hypervisor/`. Each acknowledges its operations deterministically with no real device behind it. | L1 port connect/disconnect, L2 VLAN create/add/remove/delete, L3 route configure/remove (including VRF-tagged routes), Hypervisor instance create/destroy. No real CLI: each call just returns a canned result and records a transcript line. | None. A driver method is a Python function returning `{"success": bool, ...}`. | Proving HERD's own orchestration logic: retries, the DLQ, redelivery idempotency, the reservation state machine, reconcile-on-save. You want a specific failure or a specific timing, not dialect realism. | Free and instant. Runs inside `tests/integration/`'s ephemeral gate stack; no extra container, no extra boot time. |
| 2. NOS lab | Two real network operating systems as Docker containers, checked into `infra/nos-test/`: FRRouting (Cisco-dialect Layer 3) and Nokia SR Linux (Layer 2). Driven by the real `drivers/frr_l3/`, `drivers/frr_mgmt/`, and `drivers/srl_l2/` packages. | L3 static routes (with VRF support) and Management config-apply on FRR; L2 VLAN membership (mac-vrf bridged domains) on SR Linux. Real SSH sessions, real vendor prompt state machines, real vendor error wording. | Real. FRR over SSH via vtysh (netmiko `cisco_ios`); SR Linux over SSH via `sr_cli` (netmiko `nokia_srl`). | Proving a driver's dialect handling is correct: the exact prompt sequence, the exact rejection wording, an idempotent re-apply. This is what makes the difference between "the mock said success" and "the real box actually took the command." | Two containers, no license, no external host. Boots from a public/free image in under a minute; stateless (no volumes), so a broken node is a `make nos-reset` away from clean. |
| 3. External network-simulator (not shipped) | A separate project, referenced in `docs/design/0010-emulated-gear-test-tier.md` and `PLANNED_FEATURES.md`, that would emulate Layer 1 patch-panel hardware. | Layer 1 cross-connects (the physical patching a Glimmerglass/MRV/Metamako-class matrix switch does). Not implemented in HERD today. | Not applicable; unbuilt. | Never, today: there is nothing to reach for. `drivers/frr_mgmt`'s manual live-config case (M1 in `docs/MANUAL_TESTING.md`) is the only path that still names this external lab, and only for the parts the NOS lab doesn't cover. | Needs its own Proxmox host and is not reboot-persistent (per `docs/NOS_LAB.md`'s "Why it exists" section); not something to install for a demo. |

Rule of thumb: reach for tier 1 when you are testing HERD's code, reach for tier 2
when you are testing a driver's dialect handling or want a believable live demo,
and do not reach for tier 3, it is not there yet.

## 2. Quick start: the NOS lab

This walks through booting the lab, seeding it, then building and reserving a real
topology in the UI, watching a Layer 3 static route land on the real FRR box, and
tearing it down again.

### 2.1 Boot or confirm the lab, and attach it

```bash
make nos-status
```

If both `nos-test-frr` and `nos-test-srl` show `Up ... (healthy)`, the lab is
already running; skip to the next command. Otherwise:

```bash
make nos-up
```

Then attach the lab's two containers to the dev stack's Docker network, so
HERD's own execution service can reach them by container name:

```bash
make nos-attach
```

### 2.2 Seed the lab groundwork

```bash
make seed-nos
```

This registers the real drivers and devices and cables the two placeholder DUTs to
the SR Linux switch. It is idempotent: a second run prints `Exists ...` for
everything already there. After it finishes, the following appears in HERD:

- Two driver packages on the Drivers admin page (`/admin/drivers`): **Nokia SR
  Linux L2 Switch Driver** (connection type Layer 2 Switch) and **FRRouting L3
  Switch Driver** (connection type Layer 3 Switch). A third, **NOS Lab DUT
  Placeholder Driver** (connection type Management), backs the two DUT devices;
  it is never actually invoked, since execution only ever drives the switch
  endpoint of a recorded hop, never a leaf DUT.
- Three device templates: **NOS Lab SR Linux L2 Switch**, **NOS Lab FRR L3
  Switch**, **NOS Lab DUT**.
- Four devices on the Inventory page, all `AVAILABLE`:
  - `nos-lab-srl`, the real SR Linux node (container `nos-test-srl`)
  - `nos-lab-frr`, the real FRR node (container `nos-test-frr`)
  - `nos-lab-dut-1` and `nos-lab-dut-2`, placeholder devices-under-test
- Cabling already in place: `nos-lab-dut-1:eth1` to `nos-lab-srl:ethernet-1/1`,
  and `nos-lab-dut-2:eth1` to `nos-lab-srl:ethernet-1/2`. This is the L1 hop
  groundwork the L2 VLAN-membership derivation needs (ADR 0009); it is
  deliberately the only cabling the seed creates.

`nos-lab-frr` itself is **not** pre-cabled to anything. The seed's job is the L2
groundwork; a Layer 3 route needs its own device-to-switch cable and its own
device config version, which is exactly what the next section does, in the UI,
one time.

### 2.3 Build a topology and add a Layer 3 route, in the UI

1. **Give `nos-lab-dut-1` a second port.** Open the Inventory page, select
   `nos-lab-dut-1`, and in the **Ports** section click **+ Add Port**. Its only
   existing port, `eth1`, is already cabled to the SR Linux switch, so add a new
   one named `eth2` (**Port name** field, then **Add Port**).
2. **Cable it to the FRR switch.** Open the admin **Connections** page
   (`/admin/connections`), click **Create Connection**, and connect
   `nos-lab-dut-1` port `eth2` to `nos-lab-frr` port `eth1`.
3. **Give `nos-lab-frr` a config version that maps its real interface to its
   HERD port name.** The FRR container's real Linux interface is `eth0`; the
   HERD port you just cabled is named `eth1`. Open `nos-lab-frr`'s device page,
   scroll to **Configuration history**, click **New version**, and in the
   **Config (JSON)** box enter a config whose `interfaces` entry declares
   `{"name": "eth0", "ip": "<the container's eth0 CIDR>", "zone": "trust",
   "port": "eth1"}`. This mapping (ADR 0014 addenda X-J/X-K) is what lets HERD's
   interface-level wiring check resolve `eth0` to a cabled port; without it, a
   route through `eth0` is refused before the driver is ever called
   (`l3_interface_unwired`).
4. **Build the topology.** Click **New Topology**, name it, and open it in the
   topology editor. Drag `nos-lab-dut-1` and `nos-lab-frr` from the Equipment
   Browser onto the canvas, then draw an edge between them (this becomes an L1
   cross-connect over the cable you just created).
5. **Add the route.** Select the `nos-lab-frr` node alone; a **Routing** panel
   opens (it appears only when exactly one selected node is a Layer 3 Switch).
   Fill in the bottom row's **Destination** (any test prefix, for example a
   `198.51.100.0/24` address), **Next hop** (an address on the FRR node's own
   `eth0` subnet), and **Interface** (`eth0`), then click **Add route**. Leave
   **Virtual router** blank for a default-table route.
6. **Reserve it.** Click **Reserve Topology (2 devices)**. In the **Create
   Reservation** dialog, set **Start time** and **End time**, optionally fill in
   **Purpose**, and click **Create**.
7. **Watch it go ACTIVE.** The reservation starts `PENDING_PROVISION` and flips
   to `ACTIVE` within a few seconds once the fork's routing intent is applied.
   Open the reservation's detail modal and its **Wiring** tab: under **L3 route
   pins**, the `nos-lab-frr` row reads status `ACTIVE`, "1 route".
8. **Verify on the real box.**

   ```bash
   docker exec nos-test-frr vtysh -c "show ip route static"
   ```

   The destination you entered appears with an `S>*` marker and the next hop
   and interface you set.

9. **Cancel and confirm it is gone.** Back in the reservation detail modal,
   click **Cancel** and confirm. Re-run the same `vtysh` command; the route no
   longer appears.
10. **Detach.**

    ```bash
    make nos-detach
    ```

A live run of steps 2 through 9 through the API (the same calls the UI makes)
confirmed this exact sequence end to end against the real FRR container: the
route appeared in `show ip route static` after activation and was gone after
cancellation.

### 2.4 SR Linux: the Layer 2 equivalent

The same shape works for a VLAN on the seeded SR Linux node, using the cabling
the seed already created (no extra port or cable needed): build a topology with
`nos-lab-dut-1` and `nos-lab-dut-2` as nodes and an edge directly between them
(no switch node on the canvas; the pathfinder resolves the path through
`nos-lab-srl`'s `ethernet-1/1` and `ethernet-1/2`). Reserve it, and once ACTIVE
the Wiring tab's **L2 VLAN memberships** section shows the allocated VLAN.
Verify independently with:

```bash
docker exec nos-test-srl sr_cli "show network-instance summary"
```

which lists the `mac-vrf` network instance HERD created, alongside the built-in
`mgmt` instance every SR Linux node ships with.

## 3. The mock drivers

The four mock drivers (`drivers/mock_l1/`, `mock_l2/`, `mock_l3/`,
`mock_hypervisor/`) are what `tests/integration/` uses to drive HERD's
orchestration paths without any real device. To use one against a device you
create:

1. **Upload the driver.** On the Drivers admin page, click **Upload Driver**,
   pick a **Name**, the matching **Connection Type** (Layer 1 Switch, Layer 2
   Switch, Layer 3 Switch, or Hypervisor), and the driver's zipped or gzipped
   `driver.py` plus `driver_metadata.json`.
2. **Create a template whose sections declare the three knob fields** (see
   below) as string-type fields, so the device edit form exposes them, and
   whose `driver_id` points at the driver you just uploaded.
3. **Create a device from that template.** Its `field_data` carries whatever
   the mock driver needs (for a Management-style login, `ip`/`login`/`password`;
   for a real device connection type it also carries any of the three knobs
   below).

`seedtools`'s own default template sections do not declare the mock knobs;
`tests/integration/test_l2_reconcile.py`'s ad hoc template (a `mock_fail_actions`
string field) is the worked pattern to copy.

### 3.1 The three field_data knobs

Every mock driver reads the same three optional keys from the device's
`field_data`. The execution service prefixes every field_data key with `HERD_`
before it reaches the driver's context dict, so the driver's own docstring
refers to them as `HERD_mock_fail_actions` etc; you set the bare, unprefixed key
on the device.

| Knob | Effect | Worked example |
|---|---|---|
| `mock_fail_actions` | Comma-separated action names that return `success: False` instead of succeeding. Drives the FAILED-row path. | Set `field_data.mock_fail_actions` to `add_to_vlan` on an L2 switch device, then reconcile a fork that wires a port into a VLAN: the membership lands as a FAILED row (`tests/integration/test_l2_reconcile.py`'s `test_l2_failed_add_surfaces_and_manual_retry_recovers`). Clear the field back to an empty string and retry; it converges to ACTIVE. |
| `mock_raise_actions` | Comma-separated action names that raise an exception instead of returning a result. Drives the transient-NAK path, and on exhaustion, the DLQ. | Set `field_data.mock_raise_actions` to `configure_route` on an L3 switch device before a fork save that provisions a route: the execution consumer NAKs and redelivers instead of recording a clean failure, which is what the DLQ-retention integration tests exercise. |
| `mock_sleep_ms` | Per-call sleep, in milliseconds, before the driver call returns. Exercises the sandbox's own action timeout. | Set `field_data.mock_sleep_ms` to a value larger than the execution service's configured action timeout on any mock driver's device to prove a slow driver call is killed and reported rather than hanging the consumer. |

### 3.2 How the integration tests use them

`tests/integration/test_l2_reconcile.py` and `tests/integration/test_l3_reconcile.py`
are the reference callers: each uploads its own throwaway copy of `mock_l2` or
`mock_l3` (a session-scoped fixture tars up `driver.py` and
`driver_metadata.json` straight from the `drivers/` directory, so the test always
exercises the real checked-in package, never a stale copy), creates a template
whose sections include the three knob fields, then a device whose `field_data`
starts with every knob empty. A test that wants a failure `PATCH`es the device's
`field_data` to set `mock_fail_actions` (or `mock_raise_actions`) immediately
before the action it wants to fail, then clears it again afterward so later
tests in the same session are unaffected. This is why the pattern is PATCH,
provoke, clear, not a separate device per failure mode: field_data is mutable
and the driver reads it fresh on every subprocess invocation.

## 4. Running the test tiers

| | `make nos-test-dialect` | `make nos-test-feature` |
|---|---|---|
| What it runs | The four dialect suites (`tests/nos_lab/test_nos_lab_live.py`, `test_frr_l3_driver_live.py`, `test_frr_mgmt_driver_live.py`, `test_srl_l2_driver_live.py`): one driver against one lab node over SSH, no HERD stack involved. | The two via-stack suites (`test_frr_l3_via_stack_live.py`, `test_srl_l2_via_stack_live.py`): a real device driven through HERD's reservation, fork, and execution path. |
| Needs | The lab (`make nos-up`, boots it automatically if not already up); no running HERD stack. | A running HERD stack (`make up`) plus the lab, attached; it runs `make seed-nos` itself. |
| Credential gotcha | None; these suites talk to the lab nodes directly. | Reads `SUPERADMIN_EMAIL`/`SUPERADMIN_PASSWORD` (or `SEED_EMAIL`/`SEED_PASSWORD`) from the **shell environment**, not from `.env`. A bare `make nos-test-feature` fails all four tests at the precondition with "the stack rejected the seed credentials". Export first: `export SUPERADMIN_EMAIL=$(grep -E '^SUPERADMIN_EMAIL=' .env | cut -d= -f2-)` and the same for `SUPERADMIN_PASSWORD`. |
| Runtime and output tail (observed) | 28 tests, about 110 seconds. Tail: `======================== 28 passed in 109.41s (0:01:49) ========================` | Not run in this session (would seed the lab devices into the live gate stack); nightly-only per the Makefile's own gating. |
| Where it runs in CI | Every pull request, the advisory `nos-dialect` job in `.github/workflows/ci.yml`. Advisory: branch protection requires only `backend` and `frontend`, so a red run reports without blocking a merge. | `nightly.yml` only, after the seeded e2e pass and before the locust load test (both hold reservations over the same lab devices). |
| Local gates | `make everything` runs it as its own phase, after the live-LDAP phase. `make master` does not run it. | Neither local gate runs it; it needs a running stack plus the lab together, a heavier local ask than the dialect tier. |

## 5. Troubleshooting

**A netmiko `send_config_set` call to SR Linux fails with "Failed to enter
configuration mode."** This is the `[FACTORY]` prompt trap: a factory-fresh SR
Linux node's candidate-mode prompt reads `--{ [FACTORY] + candidate private
private-admin }--`, and netmiko's mode-detection regex does not expect the
`[FACTORY]` token. The checked-in lab avoids this by applying and saving a
baseline config at boot (`infra/nos-test/srl/baseline.cli`), which clears the
tag. If you see this error, the lab's baseline did not apply; `make nos-reset`
rebuilds it from a clean image and reapplies the baseline.

**A VRF-tagged L3 route test skips with a message about the `vrf`/`dummy` kernel
modules.** The FRR node's VRF fixture (VRF `blue`, member interface `dummy0`)
needs the **Docker host's** `vrf` and `dummy` kernel modules; a container cannot
load a kernel module for itself. Fix on the host, then reset the lab:

```bash
sudo modprobe vrf dummy
make nos-reset
```

GitHub Actions runners are the common case; both CI jobs that touch the lab
already run this `modprobe` and treat its failure as non-fatal, so the
affected tests simply skip there too.

**`make nos-test-feature` fails every test with "the stack rejected the seed
credentials."** You did not export `SUPERADMIN_EMAIL`/`SUPERADMIN_PASSWORD` (or
`SEED_EMAIL`/`SEED_PASSWORD`) into the shell first; see section 4 above. The
target still detaches the lab afterward, so a failed precondition leaves
nothing attached.

**The lab is attached to the dev stack's network and you are not sure why, or
you are about to hand the stack to someone else.** Check with:

```bash
docker network inspect <project>_herd-net --format '{{range $k,$v := .Containers}}{{$v.Name}}{{"\n"}}{{end}}'
```

If `nos-test-frr` or `nos-test-srl` appear, run `make nos-detach`. A forgotten
detach does not break `make down`/`make clean` (a `docker compose down` while a
lab container is still attached prints "Resource is still in use" for the
network but still exits 0), but it does leave the network behind after the
stack itself is gone, so always pair an attach with an eventual detach.

**Tests you expect to run are skipping ("reused-stack" symptom).** If the lab
or the stack has already been through a load test or several passes, some
port-count-dependent tests elsewhere in the suite (not the NOS lab tests
themselves) skip because too few free ports remain. This is stack history, not
a lab problem; it does not affect a fresh gate seed.

**`make nos-reset` versus a lighter fix.** `nos-reset` tears down and rebuilds
both lab nodes from a clean image, discarding all node state: any VLAN, route,
or config you added by hand is gone, and the checked-in baseline and VRF
fixture are reapplied. Reach for it when a node is in a state you cannot
explain (a stuck candidate-mode session, a manually-added VLAN interfering
with a test's baseline snapshot), not as a routine step between test runs; the
lab is stateless by design, so a plain `make nos-down` followed by `make nos-up`
does the same thing.

## 6. What the lab cannot do yet

Layer 1 emulation: patching a virtual patch-panel the way a Glimmerglass,
MRV, or Metamako matrix switch would. This is the external `network-simulator`
project's job (tier 3 in the table above), and it is not shipped; it needs its
own Proxmox host and is not reboot-persistent, unlike the checked-in NOS lab.
See `PLANNED_FEATURES.md`'s extensibility section (the driver-system entry) and
`docs/design/0010-emulated-gear-test-tier.md` for where this fits in the
broader emulated-gear roadmap. Until it exists, an L1 cross-connect's dialect
handling is proven only by the mock `mock_l1` driver (tier 1) and, for a real
matrix switch, by hand against real hardware.
