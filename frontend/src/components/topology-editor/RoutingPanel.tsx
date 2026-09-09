import { Fragment, useState } from "react";
import type { Node } from "@xyflow/react";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { useTopologyStore } from "@/stores/topologyStore";
import { useDeviceConfigVersions, useDeviceConfigVersion } from "@/api/deviceConfig";
import type { DeviceNodeData, InvalidRoute, L3RouteIntent } from "@/types/topology.types";

export interface RoutingPanelProps {
  node: Node<DeviceNodeData>;
  // Pre-filtered to this node's node_id by the caller (TopologyEditorPage),
  // which already holds the flat validation result for every node.
  invalidRoutes: InvalidRoute[];
}

interface DraftRoute {
  destination: string;
  next_hop: string;
  interface: string;
  virtual_router: string;
}

const EMPTY_DRAFT: DraftRoute = { destination: "", next_hop: "", interface: "", virtual_router: "" };

// The shape a config version's Layer 3 Switch config schema stores routes in
// (services/common/herd_common/device_config.py): destination and interface,
// optional next_hop, no virtual_router grouping (that is a canvas-only,
// ADR 0014 addition).
interface DeviceConfigRoute {
  destination?: unknown;
  next_hop?: unknown;
  interface?: unknown;
}

function normalizeConfigRoutes(config: Record<string, unknown> | undefined): L3RouteIntent[] {
  const raw = config?.routes;
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => {
    const r = item as DeviceConfigRoute;
    const nextHop = typeof r.next_hop === "string" && r.next_hop.trim().length > 0 ? r.next_hop.trim() : null;
    return {
      destination: typeof r.destination === "string" ? r.destination.trim() : "",
      interface: typeof r.interface === "string" ? r.interface.trim() : "",
      next_hop: nextHop,
      // The config schema carries no virtual_router grouping; import always
      // fills it with null (ADR 0014 Decision 5's phase-1 scope).
      virtual_router: null,
    };
  });
}

const INPUT_CLASS =
  "w-full text-xs border border-gray-300 rounded px-1.5 py-1 focus:outline-none focus:border-blue-500";

/**
 * Per-switch Layer 3 routing intent editor (ADR 0014 Decision 4, issue #34
 * phase 2). Rendered by TopologyEditorPage inside a FloatingPanel titled
 * "Routing" only when exactly one device node is selected and its device's
 * connection_type is "Layer 3 Switch". Every edit (row edit, add, remove,
 * import) writes through the topology store's setNodeL3Routes (E2), which is
 * ordinary node data and rides the fork/autosave/persistence paths untouched
 * (E1). Text fields are trimmed on every change (never validated as
 * addresses client-side: the server is the sole authority, ADR 0014
 * Decision 5) and an optional field's blank value is stored as null, never
 * the empty string.
 */
export function RoutingPanel({ node, invalidRoutes }: RoutingPanelProps) {
  const setNodeL3Routes = useTopologyStore((s) => s.setNodeL3Routes);
  const routes = node.data.l3?.routes ?? [];
  const deviceId = node.data.device.id;
  const deviceName = node.data.device.name;

  const [draft, setDraft] = useState<DraftRoute>(EMPTY_DRAFT);
  const [showImportConfirm, setShowImportConfirm] = useState(false);

  // "Newest version first" (inventory's config-versions list route orders by
  // version_number desc), so a one-item page's only entry is the latest.
  const latestVersionsQuery = useDeviceConfigVersions(deviceId, 0, 1);
  const latestVersionId = latestVersionsQuery.data?.items[0]?.id;
  const hasConfigVersion = (latestVersionsQuery.data?.items.length ?? 0) > 0;
  const latestVersionQuery = useDeviceConfigVersion(deviceId, latestVersionId);

  const switchLevelReasons = invalidRoutes.filter((r) => r.index === null);
  const reasonsByIndex = new Map<number, InvalidRoute[]>();
  for (const r of invalidRoutes) {
    if (r.index === null) continue;
    const existing = reasonsByIndex.get(r.index);
    if (existing) {
      existing.push(r);
    } else {
      reasonsByIndex.set(r.index, [r]);
    }
  }

  function commit(next: L3RouteIntent[]) {
    setNodeL3Routes(node.id, next.length > 0 ? next : null);
  }

  function updateRow(index: number, field: keyof L3RouteIntent, rawValue: string) {
    const trimmed = rawValue.trim();
    const next = routes.map((route, i) => {
      if (i !== index) return route;
      if (field === "next_hop" || field === "virtual_router") {
        return { ...route, [field]: trimmed.length > 0 ? trimmed : null };
      }
      return { ...route, [field]: trimmed };
    });
    commit(next);
  }

  function handleRemove(index: number) {
    commit(routes.filter((_, i) => i !== index));
  }

  const canAdd = draft.destination.trim().length > 0 && draft.interface.trim().length > 0;

  function handleAdd() {
    if (!canAdd) return;
    const newRoute: L3RouteIntent = {
      destination: draft.destination.trim(),
      interface: draft.interface.trim(),
      next_hop: draft.next_hop.trim().length > 0 ? draft.next_hop.trim() : null,
      virtual_router: draft.virtual_router.trim().length > 0 ? draft.virtual_router.trim() : null,
    };
    commit([...routes, newRoute]);
    setDraft(EMPTY_DRAFT);
  }

  function applyImport() {
    const imported = normalizeConfigRoutes(latestVersionQuery.data?.config);
    commit(imported);
  }

  function handleImportClick() {
    if (routes.length > 0) {
      setShowImportConfirm(true);
    } else {
      applyImport();
    }
  }

  return (
    <div className="p-3 w-[420px] max-h-[70vh] overflow-y-auto text-sm">
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-gray-900">{deviceName}</span>
        <Button
          variant="outline"
          size="sm"
          onClick={handleImportClick}
          disabled={!hasConfigVersion || latestVersionQuery.isFetching}
          title={
            hasConfigVersion
              ? undefined
              : "This switch has no config version to import routes from"
          }
        >
          Import from device config
        </Button>
      </div>

      {switchLevelReasons.length > 0 && (
        <div className="mb-2 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700">
          {switchLevelReasons.map((r, i) => (
            <div key={i}>{r.reason}{r.detail ? `: ${r.detail}` : ""}</div>
          ))}
        </div>
      )}

      {routes.length === 0 ? (
        <p className="text-xs text-gray-500 mb-3">
          No routing intent. Routes come from the device&apos;s config version until you
          add some.
        </p>
      ) : (
        <table className="w-full text-xs mb-3 border-collapse">
          <thead>
            <tr className="text-gray-500 text-left">
              <th className="font-medium pb-1 pr-1">Destination</th>
              <th className="font-medium pb-1 pr-1">Next hop</th>
              <th className="font-medium pb-1 pr-1">Interface</th>
              <th className="font-medium pb-1 pr-1">Virtual router</th>
              <th className="font-medium pb-1"></th>
            </tr>
          </thead>
          <tbody>
            {routes.map((route, index) => {
              const rowReasons = reasonsByIndex.get(index) ?? [];
              return (
                <Fragment key={`route-${index}`}>
                  <tr>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Destination"
                        className={INPUT_CLASS}
                        value={route.destination}
                        onChange={(e) => updateRow(index, "destination", e.target.value)}
                      />
                    </td>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Next hop"
                        className={INPUT_CLASS}
                        value={route.next_hop ?? ""}
                        onChange={(e) => updateRow(index, "next_hop", e.target.value)}
                      />
                    </td>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Interface"
                        className={INPUT_CLASS}
                        value={route.interface}
                        onChange={(e) => updateRow(index, "interface", e.target.value)}
                      />
                    </td>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Virtual router"
                        className={INPUT_CLASS}
                        value={route.virtual_router ?? ""}
                        onChange={(e) => updateRow(index, "virtual_router", e.target.value)}
                      />
                    </td>
                    <td className="pb-1">
                      <button
                        onClick={() => handleRemove(index)}
                        aria-label={`Remove route ${index}`}
                        className="text-red-600 hover:text-red-800 text-xs px-1"
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                  {rowReasons.length > 0 && (
                    <tr>
                      <td colSpan={5} className="pb-1.5">
                        <div className="rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] text-red-700">
                          {rowReasons.map((r, i) => (
                            <div key={i}>{r.reason}{r.detail ? `: ${r.detail}` : ""}</div>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}

      <div className="border-t border-gray-200 pt-2">
        <div className="grid grid-cols-4 gap-1 mb-1.5">
          <input
            aria-label="New destination"
            placeholder="Destination"
            className={INPUT_CLASS}
            value={draft.destination}
            onChange={(e) => setDraft((d) => ({ ...d, destination: e.target.value }))}
          />
          <input
            aria-label="New next hop"
            placeholder="Next hop"
            className={INPUT_CLASS}
            value={draft.next_hop}
            onChange={(e) => setDraft((d) => ({ ...d, next_hop: e.target.value }))}
          />
          <input
            aria-label="New interface"
            placeholder="Interface"
            className={INPUT_CLASS}
            value={draft.interface}
            onChange={(e) => setDraft((d) => ({ ...d, interface: e.target.value }))}
          />
          <input
            aria-label="New virtual router"
            placeholder="Virtual router"
            className={INPUT_CLASS}
            value={draft.virtual_router}
            onChange={(e) => setDraft((d) => ({ ...d, virtual_router: e.target.value }))}
          />
        </div>
        <Button variant="secondary" size="sm" onClick={handleAdd} disabled={!canAdd}>
          Add route
        </Button>
      </div>

      <ConfirmDialog
        open={showImportConfirm}
        title="Replace routing intent?"
        description={`This replaces all ${routes.length} route${routes.length === 1 ? "" : "s"} on this switch with the latest device config version's routes.`}
        confirmLabel="Replace"
        destructive
        onConfirm={() => {
          setShowImportConfirm(false);
          applyImport();
        }}
        onCancel={() => setShowImportConfirm(false)}
      />
    </div>
  );
}
