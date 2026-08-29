import { Network, Waypoints, Cloud, Cable, type LucideIcon } from "lucide-react";

import type { NetworkElementType } from "@/types/topology.types";

// The closed v1 element vocabulary (ADR 0012 "Canvas shape"): four fixed
// entries with their own icon and label. Single source of truth, consumed by
// NetworkElementNode.tsx (canvas node chrome), ElementAttachDialog.tsx (the
// port-picker icon), and EquipmentBrowser.tsx (the palette cards); the three
// previously carried their own copy of this table.
// No type annotation on this declaration is deliberate: annotating it as
// ReadonlyArray<{ type: NetworkElementType; ... }> would WIDEN every
// element's `type` field to the full union at the type level, which makes
// the exhaustiveness check below vacuous (it would see full coverage no
// matter what the array actually contains, or is missing). Left to
// inference, `typeof NETWORK_ELEMENT_TYPES[number]["type"]` is the union of
// the LITERAL `type` values actually present, which is what the check needs.
export const NETWORK_ELEMENT_TYPES = [
  { type: "vlan_segment", label: "VLAN segment", icon: Network },
  { type: "subnet", label: "Subnet", icon: Waypoints },
  { type: "external_cloud", label: "External cloud", icon: Cloud },
  { type: "patch_trunk", label: "Patch trunk", icon: Cable },
] as const satisfies ReadonlyArray<{ type: NetworkElementType; label: string; icon: LucideIcon }>;

// Compile-time exhaustiveness: this line only type-checks if the union of
// every `type` field actually present in NETWORK_ELEMENT_TYPES is EXACTLY
// NetworkElementType, neither missing a member nor repeating one into a
// mistaken belief of coverage. A member dropped from the array (or from
// NetworkElementType itself, uncovered here) fails tsc at this line, not
// just the runtime test in networkElements.test.ts.
type CoveredType = (typeof NETWORK_ELEMENT_TYPES)[number]["type"];
const _typesExhaustive: [CoveredType] extends [NetworkElementType]
  ? [NetworkElementType] extends [CoveredType]
    ? true
    : never
  : never = true;
void _typesExhaustive;

function toRecord<V>(
  entries: ReadonlyArray<{ type: NetworkElementType; value: V }>,
): Record<NetworkElementType, V> {
  const record = {} as Record<NetworkElementType, V>;
  for (const { type, value } of entries) record[type] = value;
  return record;
}

export const ELEMENT_ICONS: Record<NetworkElementType, LucideIcon> = toRecord(
  NETWORK_ELEMENT_TYPES.map((entry) => ({ type: entry.type, value: entry.icon })),
);

export const ELEMENT_LABELS: Record<NetworkElementType, string> = toRecord(
  NETWORK_ELEMENT_TYPES.map((entry) => ({ type: entry.type, value: entry.label })),
);
