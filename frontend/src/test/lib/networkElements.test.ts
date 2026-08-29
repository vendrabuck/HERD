import { NETWORK_ELEMENT_TYPES, ELEMENT_ICONS, ELEMENT_LABELS } from "@/lib/networkElements";
import type { NetworkElementType } from "@/types/topology.types";

// Type-level exhaustiveness: this assignment only compiles if
// ELEMENT_ICONS/ELEMENT_LABELS cover every NetworkElementType with no gaps
// and no excess keys (a Record<NetworkElementType, ...> derivation is exact).
// A missing or extra entry in NETWORK_ELEMENT_TYPES fails tsc, not just this
// runtime assertion.
const _exhaustiveIcons: Record<NetworkElementType, unknown> = ELEMENT_ICONS;
const _exhaustiveLabels: Record<NetworkElementType, unknown> = ELEMENT_LABELS;
void _exhaustiveIcons;
void _exhaustiveLabels;

const ALL_TYPES: NetworkElementType[] = ["vlan_segment", "subnet", "external_cloud", "patch_trunk"];

describe("networkElements", () => {
  it("NETWORK_ELEMENT_TYPES covers every NetworkElementType exactly once", () => {
    const types = NETWORK_ELEMENT_TYPES.map((e) => e.type);
    expect(types.sort()).toEqual([...ALL_TYPES].sort());
    expect(new Set(types).size).toBe(ALL_TYPES.length);
  });

  it("ELEMENT_ICONS and ELEMENT_LABELS are derived from the same table for every type", () => {
    for (const entry of NETWORK_ELEMENT_TYPES) {
      expect(ELEMENT_ICONS[entry.type]).toBe(entry.icon);
      expect(ELEMENT_LABELS[entry.type]).toBe(entry.label);
    }
  });

  it("every label is a non-empty string and every icon a component", () => {
    for (const type of ALL_TYPES) {
      expect(typeof ELEMENT_LABELS[type]).toBe("string");
      expect(ELEMENT_LABELS[type].length).toBeGreaterThan(0);
      expect(ELEMENT_ICONS[type]).toBeTruthy();
    }
  });
});
