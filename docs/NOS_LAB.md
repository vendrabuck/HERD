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
| `srl` | `ghcr.io/nokia/srlinux:latest` | Layer 2 target (mac-vrf / bridged subinterfaces) | `nokia_srl` | `${HERD_TEST_SRL_PORT:-2223}` (SSH) | `admin` / `NokiaSrl1!` |
| `frr` | built from `infra/nos-test/frr/Dockerfile` (`frrouting/frr:latest` base) | Layer 3 / Cisco-dialect target (vtysh, IOS-style syntax) | `cisco_ios` | `${HERD_TEST_FRR_PORT:-2224}` (SSH) | `netadmin` / `netadmin` |

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

- `make nos-up` , start both nodes and wait until healthy (builds the FRR
  image first if needed)
- `make nos-down` , stop and remove both nodes
- `make nos-status` , show container status
- `make nos-logs` , tail both nodes' logs
- `make nos-reset` , tear down and rebuild from scratch

None of these run as part of `make test`, `make master`, or `make everything`;
this phase is opt-in only, so a host that never runs it pays nothing for it.

## Tests

- `tests/unit/test_nos_lab_compose.py` , static, runs in CI with no lab: pins
  the compose project name, the stateless (no-named-volumes) property, that
  both services declare a healthcheck, that the published ports do not
  collide with the dev or gate compose files' ports, and the shape of the
  checked-in SR Linux baseline file.
- `tests/nos_lab/test_nos_lab_live.py` , opt-in, needs the lab running.
  Skips automatically when the lab is not reachable. Set
  `HERD_TEST_NOS_REQUIRED=1` to turn an unreachable lab into a hard failure
  instead of a skip (the same convention
  `services/auth/tests/test_ldap_service_live.py` uses with
  `HERD_TEST_LDAP_REQUIRED`), so a CI job that asks for these tests
  explicitly never silently no-ops. Run it with:

  ```bash
  make nos-up
  HERD_TEST_NOS_REQUIRED=1 uv run pytest tests/nos_lab/ -v
  ```

  It creates a VLAN (SR Linux) and a static route (FRR) with a random,
  per-run-unique identifier, verifies each change independently of the
  netmiko session that made it (a separate `docker exec` into the
  container, never the driver's own status method), and cleans up
  afterward so the lab stays re-runnable without a reset.
- `tests/nos_lab/test_frr_l3_driver_live.py` , opt-in, same gating as above,
  drives the checked-in `drivers/frr_l3` Layer 3 Switch reference driver
  (not raw netmiko) against the FRR node, verifying every route change
  independently via `docker exec ... vtysh` and covering both route forms
  plus the driver's idempotency behavior; see docs/DRIVERS.md's "FRR
  reference driver" section.
- `tests/nos_lab/test_srl_l2_driver_live.py` , the same opt-in gating,
  scoped to the SR Linux node only. Exercises the real
  `drivers/srl_l2/driver.py` (Nokia SR Linux Layer 2 Switch driver, see
  `docs/DRIVERS.md`) end to end: create a VLAN, add a port to it tagged and
  untagged, verify the mac-vrf and the port binding independently via
  `sr_cli` (never through the driver's own session), prove `create_vlan`
  idempotency and `delete_vlan`-on-a-missing-VLAN, then remove and delete
  and verify both are gone.

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
