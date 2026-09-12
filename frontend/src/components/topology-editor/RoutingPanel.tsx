import { Fragment, useEffect, useState } from "react";
import type { Node } from "@xyflow/react";
import toast from "react-hot-toast";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { useTopologyStore } from "@/stores/topologyStore";
import { useDeviceConfigVersions, useDeviceConfigVersion } from "@/api/deviceConfig";
import { errorDetail } from "@/lib/errors";
import { l3IsMalformed, l3RoutesOf } from "@/lib/canvasNodes";
import {
  matchRouteProblems,
  routeProblemFields,
  type ResolvedRouteProblem,
  type RouteProblemField,
} from "@/lib/l3";
import type { DeviceNodeData, L3RouteIntent } from "@/types/topology.types";

export interface RoutingPanelProps {
  node: Node<DeviceNodeData>;
  // Pre-filtered to this node's node_id by the caller (TopologyEditorPage),
  // already RESOLVED (review fix F4: route VALUES, not raw indexes) and
  // including both blocking and informational (l3_duplicate_route) entries;
  // this panel does its own row matching via `matchRouteProblems`.
  problems: ResolvedRouteProblem[];
}

interface DraftRoute {
  destination: string;
  next_hop: string;
  interface: string;
  virtual_router: string;
}

const EMPTY_DRAFT: DraftRoute = { destination: "", next_hop: "", interface: "", virtual_router: "" };

// Cabling's `_MAX_FIELD_LENGTH` (services/cabling/app/services/l3_intent.py):
// review fix F2. Enforced in the change handlers themselves (truncating the
// stored value), not just the `maxLength` HTML attribute, since the latter
// only stops a user's own keystrokes, never a scripted or pasted value.
const MAX_FIELD_LENGTH = 64;

function clampField(value: string): string {
  return value.length > MAX_FIELD_LENGTH ? value.slice(0, MAX_FIELD_LENGTH) : value;
}

function toDraftRoute(route: L3RouteIntent): DraftRoute {
  return {
    destination: route.destination,
    next_hop: route.next_hop ?? "",
    interface: route.interface,
    virtual_router: route.virtual_router ?? "",
  };
}

function draftToRoute(draft: DraftRoute): L3RouteIntent {
  const nextHop = draft.next_hop.trim();
  const virtualRouter = draft.virtual_router.trim();
  return {
    destination: draft.destination.trim(),
    interface: draft.interface.trim(),
    next_hop: nextHop.length > 0 ? nextHop : null,
    virtual_router: virtualRouter.length > 0 ? virtualRouter : null,
  };
}

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

// The same box, outlined red because a blocking reason on this row is ABOUT this
// field (ADR 0014 addendum X-I, issue #755). The reason line below the row still
// names the reason; this only says WHICH box it is talking about, which matters
// most for the VRF reasons, where "unknown virtual router" and "interface bound
// to a virtual router" point at different columns.
const INPUT_CLASS_INVALID =
  "w-full text-xs border border-red-400 bg-red-50 rounded px-1.5 py-1 focus:outline-none focus:border-red-500";

function inputClass(fields: Set<RouteProblemField>, field: RouteProblemField): string {
  return fields.has(field) ? INPUT_CLASS_INVALID : INPUT_CLASS;
}

function ReasonLine({ problem }: { problem: ResolvedRouteProblem }) {
  // Review fix F1: l3_duplicate_route is informational (cabling's
  // route_causes_invalid excludes it from `valid` and from the save-gate
  // refusal), so it renders amber with friendly text, never red/raw.
  if (problem.reason === "l3_duplicate_route") {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
        Duplicate of another route on this switch
      </div>
    );
  }
  return (
    <div className="rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] text-red-700">
      {problem.reason}
      {problem.detail ? `: ${problem.detail}` : ""}
    </div>
  );
}

/**
 * Per-switch Layer 3 routing intent editor (ADR 0014 Decision 4, issue #34
 * phase 2). Rendered by TopologyEditorPage inside a FloatingPanel titled
 * "Routing" only when exactly one device node is selected and it is a Layer
 * 3 Switch. Every commit (row edit on blur/Enter, add, remove, import)
 * writes through the topology store's setNodeL3Routes (E2), which is
 * ordinary node data and rides the fork/autosave/persistence paths untouched
 * (E1). Text fields are never validated as addresses client-side (the
 * server is the sole authority, ADR 0014 Decision 5); an optional field's
 * blank value is stored as null, never the empty string.
 *
 * Existing rows edit in LOCAL draft state, committing to the store only on
 * blur or Enter (review fix F2): the previous per-keystroke commit re-mapped
 * every canvas node and re-stringified the whole canvas in
 * useForkAutosave's signature on every character typed, and could write an
 * empty destination/interface straight to the store, the exact shape the
 * server refuses as 422 `l3_intent_malformed`.
 */
export function RoutingPanel({ node, problems }: RoutingPanelProps) {
  const setNodeL3Routes = useTopologyStore((s) => s.setNodeL3Routes);
  const deviceId = node.data.device.id;
  const deviceName = node.data.device.name;

  // Review fix F6: a malformed `data.l3` (present, but `routes` is not an
  // array) gets its own repair affordance instead of being read as "no
  // routes" and silently losing whatever the malformed value was pointing
  // at. `l3RoutesOf` below already never throws on this shape either way.
  const malformed = l3IsMalformed(node.data);
  const routes = l3RoutesOf(node.data);

  const [rowDrafts, setRowDrafts] = useState<DraftRoute[]>(() => routes.map(toDraftRoute));
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({});
  // Resyncs local per-row drafts whenever the committed `routes` changes for
  // ANY reason (a commit from this panel, Add, Remove, or Import): the only
  // writer of `routes` is this panel itself (single-user, single-panel
  // editing), so every such change is either this effect catching up to a
  // write this component JUST made (a no-op resync) or a wholesale
  // replacement (Import) that should discard any stale in-progress edit.
  useEffect(() => {
    // Resyncing local draft rows from the committed `routes` this panel
    // itself just wrote (or replaced via Import) is the intended
    // synchronization here, not an avoidable derived-state anti-pattern.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setRowDrafts(routes.map(toDraftRoute));
    setRowErrors({});
  }, [routes]);

  const [draft, setDraft] = useState<DraftRoute>(EMPTY_DRAFT);
  const [showImportConfirm, setShowImportConfirm] = useState(false);
  const [pendingImportRoutes, setPendingImportRoutes] = useState<L3RouteIntent[] | null>(null);

  // "Newest version first" (inventory's config-versions list route orders by
  // version_number desc), so a one-item page's only entry is the latest.
  // Review fix F10: this cheap list query is the ONLY thing fetched on
  // selection; the full config version body is fetched lazily, only on an
  // actual Import click (see handleImportClick below), so selecting an L3
  // switch never pays for a config-body fetch it may not need.
  const latestVersionsQuery = useDeviceConfigVersions(deviceId, 0, 1);
  const latestVersionId = latestVersionsQuery.data?.items[0]?.id;
  const hasConfigVersion = latestVersionId !== undefined;
  const latestVersionQuery = useDeviceConfigVersion(deviceId, latestVersionId, { enabled: false });

  const { switchLevel, perRow } = matchRouteProblems(routes, problems);

  function commitRow(index: number) {
    const rowDraft = rowDrafts[index];
    if (!rowDraft) return;
    if (rowDraft.destination.trim().length === 0 || rowDraft.interface.trim().length === 0) {
      // Review fix F2: refused inline; the store keeps the last committed
      // value (no setNodeL3Routes call at all), so an empty destination or
      // interface can never reach the fork save's 422 l3_intent_malformed.
      setRowErrors((e) => ({ ...e, [index]: "Destination and interface are required" }));
      return;
    }
    const committed = draftToRoute(rowDraft);
    const next = routes.map((r, i) => (i === index ? committed : r));
    setNodeL3Routes(node.id, next);
  }

  function updateRowField(index: number, field: keyof DraftRoute, rawValue: string) {
    const value = clampField(rawValue);
    setRowDrafts((prev) => prev.map((d, i) => (i === index ? { ...d, [field]: value } : d)));
  }

  function handleRowKeyDown(index: number, e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitRow(index);
    }
  }

  function handleRemove(index: number) {
    setNodeL3Routes(
      node.id,
      routes.filter((_, i) => i !== index),
    );
  }

  function handleRemoveAllMalformed() {
    setNodeL3Routes(node.id, []);
  }

  const canAdd = draft.destination.trim().length > 0 && draft.interface.trim().length > 0;

  function handleAdd() {
    if (!canAdd) return;
    setNodeL3Routes(node.id, [...routes, draftToRoute(draft)]);
    setDraft(EMPTY_DRAFT);
  }

  // Review fix F3/F10: fetches the config version body only now, on click.
  // Guards against every way `applyImport` used to wipe routes: an error
  // toasts and changes nothing; no `routes` key (or an empty list) toasts
  // "no routes" and changes nothing; only a genuinely non-empty routes list
  // ever reaches setNodeL3Routes, whether directly (empty table) or after
  // confirmation (table already had rows).
  async function handleImportClick() {
    const result = await latestVersionQuery.refetch();
    if (result.isError) {
      toast.error(errorDetail(result.error, "Failed to load the device config version"));
      return;
    }
    const imported = normalizeConfigRoutes(result.data?.config);
    if (imported.length === 0) {
      toast.error("Latest config version has no routes");
      return;
    }
    if (routes.length > 0) {
      setPendingImportRoutes(imported);
      setShowImportConfirm(true);
    } else {
      setNodeL3Routes(node.id, imported);
    }
  }

  if (malformed) {
    return (
      <div className="p-3 w-[420px] text-sm">
        <div className="mb-2 font-semibold text-gray-900">{deviceName}</div>
        <div className="rounded border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700 mb-2">
          Routing intent on this node is malformed
        </div>
        <Button variant="danger" size="sm" onClick={handleRemoveAllMalformed}>
          Remove all
        </Button>
      </div>
    );
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
          {latestVersionQuery.isFetching ? "Importing..." : "Import from device config"}
        </Button>
      </div>

      {switchLevel.length > 0 && (
        <div className="mb-2 space-y-1">
          {switchLevel.map((p, i) => (
            <ReasonLine key={i} problem={p} />
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
            {routes.map((_route, index) => {
              const rowDraft = rowDrafts[index] ?? EMPTY_DRAFT;
              const rowReasons = perRow.get(index) ?? [];
              const invalidFields = routeProblemFields(rowReasons);
              const rowError = rowErrors[index];
              return (
                <Fragment key={`route-${index}`}>
                  <tr>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Destination"
                        className={inputClass(invalidFields, "destination")}
                        maxLength={MAX_FIELD_LENGTH}
                        value={rowDraft.destination}
                        onChange={(e) => updateRowField(index, "destination", e.target.value)}
                        onBlur={() => commitRow(index)}
                        onKeyDown={(e) => handleRowKeyDown(index, e)}
                      />
                    </td>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Next hop"
                        className={inputClass(invalidFields, "next_hop")}
                        maxLength={MAX_FIELD_LENGTH}
                        value={rowDraft.next_hop}
                        onChange={(e) => updateRowField(index, "next_hop", e.target.value)}
                        onBlur={() => commitRow(index)}
                        onKeyDown={(e) => handleRowKeyDown(index, e)}
                      />
                    </td>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Interface"
                        className={inputClass(invalidFields, "interface")}
                        maxLength={MAX_FIELD_LENGTH}
                        value={rowDraft.interface}
                        onChange={(e) => updateRowField(index, "interface", e.target.value)}
                        onBlur={() => commitRow(index)}
                        onKeyDown={(e) => handleRowKeyDown(index, e)}
                      />
                    </td>
                    <td className="pr-1 pb-1">
                      <input
                        aria-label="Virtual router"
                        className={inputClass(invalidFields, "virtual_router")}
                        maxLength={MAX_FIELD_LENGTH}
                        value={rowDraft.virtual_router}
                        onChange={(e) => updateRowField(index, "virtual_router", e.target.value)}
                        onBlur={() => commitRow(index)}
                        onKeyDown={(e) => handleRowKeyDown(index, e)}
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
                  {rowError && (
                    <tr>
                      <td colSpan={5} className="pb-1.5">
                        <div className="rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] text-red-700">
                          {rowError}
                        </div>
                      </td>
                    </tr>
                  )}
                  {rowReasons.length > 0 && (
                    <tr>
                      <td colSpan={5} className="pb-1.5 space-y-1">
                        {rowReasons.map((p, i) => (
                          <ReasonLine key={i} problem={p} />
                        ))}
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
            maxLength={MAX_FIELD_LENGTH}
            value={draft.destination}
            onChange={(e) => setDraft((d) => ({ ...d, destination: clampField(e.target.value) }))}
          />
          <input
            aria-label="New next hop"
            placeholder="Next hop"
            className={INPUT_CLASS}
            maxLength={MAX_FIELD_LENGTH}
            value={draft.next_hop}
            onChange={(e) => setDraft((d) => ({ ...d, next_hop: clampField(e.target.value) }))}
          />
          <input
            aria-label="New interface"
            placeholder="Interface"
            className={INPUT_CLASS}
            maxLength={MAX_FIELD_LENGTH}
            value={draft.interface}
            onChange={(e) => setDraft((d) => ({ ...d, interface: clampField(e.target.value) }))}
          />
          <input
            aria-label="New virtual router"
            placeholder="Virtual router"
            className={INPUT_CLASS}
            maxLength={MAX_FIELD_LENGTH}
            value={draft.virtual_router}
            onChange={(e) => setDraft((d) => ({ ...d, virtual_router: clampField(e.target.value) }))}
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
          if (pendingImportRoutes) {
            setNodeL3Routes(node.id, pendingImportRoutes);
            setPendingImportRoutes(null);
          }
        }}
        onCancel={() => {
          setShowImportConfirm(false);
          setPendingImportRoutes(null);
        }}
      />
    </div>
  );
}
