"""Seeding for a running HERD stack: users, drivers, templates, devices, ports,
L1/L2 switches, cabling, groups, topologies.

Creates:
  - 50 admin users and 1000 regular users
  - 4 drivers: Network OS Management, Endpoint Management, L1 Switch Driver, L2 Switch Driver
  - 28 DUT device templates (24 generic network devices across 6 invented vendors
    in 6 classes: router, switch, firewall, load balancer, server, storage + 4 client OS)
  - 4 infra templates: 1 L1 switch, 2 L2 switch (incl. Cisco Catalyst 6509), 1 port template
  - 5000 DUT devices distributed evenly across templates (variable port counts per template)
  - 29 L1 switches: 27 edge (9 per lab) + 2 hub, each with 256 backplane ports (0/slot/port)
  - 30 L2 edge switches (10 per lab), each with 48 access ports
  - L1 cabling: DUT-to-edge (eth1-eth5/eth1-eth2) + 54 edge-to-hub + 1 hub-to-hub
  - L2 cabling: DUT-to-L2 (network-device eth6-eth7, client eth3) round-robin across L2 switches
  - 6 isolated demo devices (zero cabling, TEST-NET-1 IPs) as known-unreachable endpoints
  - 60 demo lab topologies: 50 valid named shapes (chain, star, ring, mesh, dual-homed)
    plus 10 deliberately invalid (no_path and missing_device) for editor demos
  - 3 device groups: Lab Alpha, Lab Bravo, Lab Charlie (DUTs + L1 + L2 switches)
  - 5 user groups: Lab Admins, Lab Managers, Lab Alpha Tier 1, Lab Bravo Tier 1, Lab Charlie Tier 1
  - Device group permissions: Lab Alpha Tier 1 to Lab Alpha, Lab Bravo Tier 1 to Lab Bravo,
    Lab Charlie Tier 1 to Lab Charlie; admin groups get no permissions (admin role sees all)

Re-runnable: every subcommand skips resources that already exist.

Run it as `python -m seedtools <subcommand>` (see seedtools/cli.py); `make seed`,
`make seed-frr` and `make seed-nos` are the Makefile front doors. The modules
split along the seams of the retired root module seed_devices_public.py: client
(base URL, credential resolution, login, paginated reads), catalog (static
templates and the generated fleet), drivers, inventory, groups, cabling,
topologies, users, acl_fixtures, frr_demo, nos_lab.
"""
