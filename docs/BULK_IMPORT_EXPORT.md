# Bulk import and export

HERD can round-trip devices, templates, and topologies as CSV or JSON files.
This is the repeatable path for onboarding a fleet into a fresh instance and for
migrating between two instances. Import is a batched caller of the existing
creation logic, not a back door around it: every row is validated against the
same Pydantic schemas and runs through the same create/update service functions
and RBAC as the interactive endpoints.

## Why references travel by name, not UUID

Services reference each other by UUID, and those UUIDs do not match across two
HERD instances. So export emits a resource's foreign references by natural
identity (a name), and import resolves each name back to the target instance's
UUID. The identity keys are:

| Resource | Identity key | Foreign references resolved by |
|---|---|---|
| device | `name` | template by `template_name`; resolved against this instance's templates |
| template | `name` | driver by `driver_name`; resolved against this instance's drivers |
| topology | `name` | each canvas device node by device `name`; resolved against this instance's inventory over HTTP |

Import order matters: import templates before devices (so a device's
`template_name` resolves), and import devices before topologies (so a topology's
canvas device names resolve).

## Endpoints

Devices and templates are owned by the inventory service; topologies by the
cabling service. There is no cross-schema bulk path.

| Method and path | Resource | RBAC |
|---|---|---|
| `GET /api/inventory/devices/export?format=csv\|json` | devices | admin |
| `POST /api/inventory/devices/import?format=csv\|json&dry_run=true\|false` | devices | admin |
| `GET /api/inventory/templates/export?format=csv\|json` | templates | admin |
| `POST /api/inventory/templates/import?format=csv\|json&dry_run=true\|false` | templates | admin |
| `GET /api/cabling/topologies/export?format=csv\|json` | topologies | any authenticated user |
| `POST /api/cabling/topologies/import?format=csv\|json&dry_run=true\|false` | topologies | any authenticated user |

Import endpoints accept a single multipart file field named `file`. `format`
defaults to `json`. `dry_run` defaults to `false`.

## Dry run and the per-row report

Every import returns a `BulkImportReport`:

```json
{
  "dry_run": false,
  "total": 3,
  "created": 1,
  "updated": 1,
  "skipped": 0,
  "rejected": 1,
  "rows": [
    { "row": 0, "action": "create", "identity": "switch-01", "reason": null },
    { "row": 1, "action": "update", "identity": "switch-02", "reason": null },
    { "row": 2, "action": "reject", "identity": "switch-03", "reason": "template not found by name: 'Nope'" }
  ]
}
```

- `create`: a new resource would be (dry-run) or was (commit) created.
- `update`: a resource matched by identity would be or was updated.
- `skip`: reserved for byte-identical matches; not currently emitted by the
  inventory importers, which always update on an identity match.
- `reject`: the row failed validation or reference resolution. The `reason`
  explains why. A reject never aborts the batch; every other row is still
  processed. Import is per-row transactional: a rejected row is rolled back and
  leaves no partial write.

With `dry_run=true`, full parsing, validation, and reference resolution run and
the same report is returned, but nothing is written. Run a dry run first to
preview a migration.

## File schemas

### Devices

JSON export is an object with a `resource`, a `version`, and an `items` list;
import accepts that object or a bare list of item objects.

```json
{
  "resource": "devices",
  "version": 1,
  "items": [
    {
      "name": "switch-01",
      "template_name": "Arista 7050",
      "topology_type": "PHYSICAL",
      "status": "AVAILABLE",
      "field_data": { "rack": "A1", "serial": "JPE123" },
      "poll_interval_seconds": null
    }
  ]
}
```

CSV uses the column order `name, template_name, topology_type, status,
field_data, poll_interval_seconds`. `field_data` is a JSON object encoded into a
single cell. `topology_type` is `PHYSICAL` or `CLOUD`; `status` is one of
`AVAILABLE`, `RESERVED`, `OFFLINE`, `MAINTENANCE`.

### Templates

```json
{
  "resource": "templates",
  "version": 1,
  "items": [
    {
      "name": "Arista 7050",
      "template_type": "device",
      "driver_name": "arista-eos",
      "exclusive": true,
      "icon": null,
      "description": "Top-of-rack switch",
      "vendor": "Arista",
      "model": "7050",
      "part_number": "DCS-7050",
      "sections": [
        {
          "name": "General",
          "fields": [
            { "key": "rack", "label": "Rack", "type": "string", "required": false }
          ]
        }
      ],
      "poll_interval_seconds": null
    }
  ]
}
```

CSV column order: `name, template_type, driver_name, exclusive, icon,
description, vendor, model, part_number, sections, poll_interval_seconds`.
`sections` is the JSON section/field definition list encoded into a single cell.
`driver_name` may be empty for a port template; a device template must resolve a
driver and supply `vendor` and `model`.

### Topologies

JSON is the lossless round-trip format. Each item is a topology `name` plus its
`canvas` (the React Flow nodes and edges). On export, each node's
`data.device.id` is replaced by the device `name` under `data.device.name`; on
import, that name resolves back to a local device id.

Unlike devices and templates, topology import is **create-only today**: there is no
name-based update-matching branch, so every import row always creates a new topology,
even when a topology with the same `name` already exists. Re-importing the same file
creates duplicate topologies rather than updating the original. The `identity` field on
each row's report entry is still the topology `name`, but the `action` will always read
`create`, never `update`.

```json
{
  "resource": "topologies",
  "version": 1,
  "items": [
    {
      "name": "Spine-Leaf Lab",
      "canvas": {
        "nodes": [
          { "id": "n1", "data": { "device": { "name": "spine-01" }, "label": "spine-01" } },
          { "id": "n2", "data": { "device": { "name": "leaf-01" }, "label": "leaf-01" } }
        ],
        "edges": [
          { "id": "e1", "source": "n1", "target": "n2", "data": { "layer": "L1" } }
        ]
      }
    }
  ]
}
```

CSV is a flat edge list with the column order `topology_name, source_device,
source_port, target_device, target_port, layer`. Each row is one canvas edge,
with the endpoint devices named. CSV is a convenience view of the wiring graph:
it does not carry isolated nodes (a device node with no edges), so use JSON when
a topology has unconnected devices.

## Validation on import

A topology import runs the existing `build_adjacency_graph` and validate path
before any write, exactly as the interactive topology editor does. An edge whose
two devices have no physical path through the cabling graph is rejected with a
`topology validation failed` reason, and the topology is not created. A canvas
node whose device name does not exist in the target instance's inventory is
rejected with an `unresolved device names` reason.

## Out of scope

Reservations, ACL grants, users, and notification preferences are not part of
bulk import/export (inventory and topology only). This is point-in-time file
import/export, not a live sync between instances. Driver packages keep their own
upload path (see [DRIVERS.md](DRIVERS.md)).
