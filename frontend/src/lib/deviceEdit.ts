import type { DeviceStatus, DeviceUpdate, TopologyType } from "@/types/device.types";

// The editable fields of the device page form (issue #1020).
export interface DeviceEditFields {
  name: string;
  topology_type: TopologyType;
  status: DeviceStatus;
  field_data: Record<string, unknown>;
}

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The device update to send: only the fields the admin changed since the edit
 * began (issue #1020). `baseline` is the device as it stood when Edit was
 * clicked, so a field the admin left alone is never sent, and a value another
 * writer stored meanwhile (provisioning flipping status to RESERVED) is not
 * overwritten with the snapshot. `field_data` is replaced as a whole when any
 * key in it changed, since the update endpoint stores the object it is given.
 * An empty object means there is nothing to save. The name is compared and sent
 * trimmed.
 */
export function deviceEditPayload(
  baseline: DeviceEditFields,
  form: DeviceEditFields,
): DeviceUpdate {
  const out: DeviceUpdate = {};
  const name = form.name.trim();
  if (name !== baseline.name) out.name = name;
  if (form.topology_type !== baseline.topology_type) out.topology_type = form.topology_type;
  if (form.status !== baseline.status) out.status = form.status;
  if (!sameJson(form.field_data, baseline.field_data)) out.field_data = form.field_data;
  return out;
}
