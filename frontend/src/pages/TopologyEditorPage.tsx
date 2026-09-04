import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  ConnectionMode,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type OnConnect,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import toast from "react-hot-toast";

import { useTopology, useUpdateTopology, useRestoreVersion } from "@/api/topologies";
import {
  useReservations,
  useUpdateReservation,
  useReservationFork,
  useSaveReservationFork,
  forkConflictDetail,
} from "@/api/reservations";
import { useForkVersionPreview } from "@/hooks/useForkVersionPreview";
import { usePathfindPairs, type DevicePair } from "@/api/connections";
import { useAIStatus } from "@/api/ai";
import { hydrateAndLoadCanvas } from "@/lib/canvasHydration";
import {
  isDynamicPlaceholder,
  isNetworkElement,
  isDeviceNode,
  collectCanvasDeviceIds,
} from "@/lib/canvasNodes";
import { useTopologyStore } from "@/stores/topologyStore";
import { useForkAutosave } from "@/hooks/useForkAutosave";
import { EquipmentBrowser } from "@/components/equipment-browser/EquipmentBrowser";
import { FloatingPanel } from "@/components/ui/FloatingPanel";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";
import { WiringDialog, type SessionConnection } from "@/components/topology-editor/WiringDialog";
import {
  ElementAttachDialog,
  type ElementAttachSelection,
} from "@/components/topology-editor/ElementAttachDialog";
import { QuickConnectPopover } from "@/components/topology-editor/QuickConnectPopover";
import { AIDialog } from "@/components/topology-editor/AIDialog";
import { AICommitDialog } from "@/components/topology-editor/AICommitDialog";
import { AIProposalBar } from "@/components/topology-editor/AIProposalBar";
import { LiveEditBar } from "@/components/topology-editor/LiveEditBar";
import { AsBuiltBar } from "@/components/topology-editor/AsBuiltBar";
import { ForkSaveResultToast } from "@/components/topology-editor/ForkSaveResultToast";
import { ForkConflictDialog } from "@/components/topology-editor/ForkConflictDialog";
import { ForkHistoryPanel } from "@/components/topology-editor/ForkHistoryPanel";
import { ForkVersionPreviewBar } from "@/components/topology-editor/ForkVersionPreviewBar";
import { HistoryPanel } from "@/components/topology-editor/HistoryPanel";
import { VersionDiffDialog } from "@/components/topology-editor/VersionDiffDialog";
import { RestoreConfirmDialog } from "@/components/topology-editor/RestoreConfirmDialog";
import { Modal } from "@/components/ui/Modal";
import { useCreateTemplateFromTopology } from "@/api/topologyTemplates";
import apiClient from "@/api/client";
import { DeviceNode } from "@/components/topology-editor/nodes/DeviceNode";
import { DynamicPlaceholderNode } from "@/components/topology-editor/nodes/DynamicPlaceholderNode";
import { NetworkElementNode } from "@/components/topology-editor/nodes/NetworkElementNode";
import { LayerEdge } from "@/components/topology-editor/edges/LayerEdge";
import { BundledEdge } from "@/components/topology-editor/edges/BundledEdge";
import { groupEdgesForRender, isAnnotationEdge } from "@/components/topology-editor/edges/groupEdgesForRender";
import { LAYER_OPTIONS } from "@/components/topology-editor/edges/layerStyles";
import { resolveEdgeStroke } from "@/components/topology-editor/edges/edgeStatus";
import { genId } from "@/lib/id";
import type { Device, TopologyType } from "@/types/device.types";
import type { AIGenerateResponse } from "@/types/ai.types";
import type { ForkConflictDetail } from "@/types/reservation.types";
import type {
  CanvasData,
  DeviceNodeData,
  DynamicPlaceholderNodeData,
  EdgeLayerType,
  LayerEdgeData,
  NetworkElementNodeData,
  NetworkElementType,
  TopologyVersion,
  TopologyVersionDetail,
} from "@/types/topology.types";

interface PendingConnection {
  connection: Connection;
  sourceDeviceId: string;
  sourceDeviceName: string;
  sourceTopologyType: TopologyType;
  targetDeviceId: string;
  targetDeviceName: string;
  targetTopologyType: TopologyType;
  // Which surface is currently showing for this pending connection (issue
  // #517 review round 3 item 12.4). Replaces a separate popoverEscalated
  // boolean plus its own four reset call sites: since this lives ON the
  // pending connection itself, clearing pendingConnection to null (every
  // confirm/cancel path already does that) resets it for free, with nothing
  // extra to remember to reset.
  surface: "quick" | "dialog";
}

// ADR 0012 "Editing surface": a device-to-element line opens ElementAttachDialog
// instead of either wiring surface above. Kept as its own state (not folded
// into PendingConnection) since the shapes genuinely differ: one element id/
// label/type on one side instead of a second device.
interface PendingElementAttach {
  connection: Connection;
  deviceId: string;
  deviceName: string;
  deviceTopologyType: TopologyType;
  elementNodeId: string;
  elementId: string;
  elementLabel: string;
  elementType: NetworkElementType;
}

const nodeTypes = {
  deviceNode: DeviceNode,
  dynamicPlaceholderNode: DynamicPlaceholderNode,
  networkElementNode: NetworkElementNode,
};
const edgeTypes = { layerEdge: LayerEdge, bundledEdge: BundledEdge };

// React Flow annotates edges it manages as a controlled component with its
// own transient fields (selected, animated, style, zIndex); none of these
// are application data HERD ever intentionally sets (LayerEdge computes its
// own styling from `data`, never from `edge.style`). Persisting them is a
// real bug, not a cosmetic one (issue #517 review item 1): a bundle that
// picked up `selected: true` via a click has no way for React Flow to ever
// ask for it to be cleared again (see groupEdgesForRender's doc comment), so
// without this strip a save could bake a stale `selected: true` into
// canvas_data and a later reload would render that edge pre-selected.
function stripTransientEdgeFields(edge: Edge<LayerEdgeData>): Edge<LayerEdgeData> {
  const { selected: _selected, animated: _animated, style: _style, zIndex: _zIndex, ...rest } = edge;
  return rest;
}

const LAYER_DESCRIPTIONS: Record<EdgeLayerType, string> = {
  L1: "Physical / fiber",
  L2: "Ethernet / VLAN",
  L3: "IP routing",
};

function TopologyEditorInner() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { screenToFlowPosition } = useReactFlow();
  const { data: topology, isLoading } = useTopology(id);
  const updateTopology = useUpdateTopology();

  // Reservation-bound ("live edit") mode: entered via
  // /topology/:id?reservationId=... from a reservation's detail modal. In this
  // mode the canvas is the reservation's editable fork (ADR 0006 Decision 6), NOT
  // the parent topology: edits autosave as loose fork drafts, committing saves a
  // fork version (never touching the parent's TopologyVersion history), and the
  // device-set PATCH still drives incremental provisioning.
  const reservationId = searchParams.get("reservationId");
  const { data: reservations } = useReservations();
  const liveReservation = useMemo(
    () => reservations?.find((r) => r.id === reservationId) ?? null,
    [reservations, reservationId],
  );
  const isLiveEdit = !!reservationId;
  const updateReservation = useUpdateReservation();
  const { data: fork } = useReservationFork(isLiveEdit ? reservationId : null);
  const saveFork = useSaveReservationFork();
  const [isCommitting, setIsCommitting] = useState(false);
  const [forkLoaded, setForkLoaded] = useState(false);
  const [saveConflict, setSaveConflict] = useState<ForkConflictDetail | null>(null);

  // An ARCHIVED fork is the frozen as-built record of an ended reservation: the
  // canvas renders read-only. This is the authoritative signal (the fork is
  // archived by the teardown paths), so mutations key off it directly. Kept
  // separate from the broader isReadOnly below (which ALSO locks during a
  // fork-history preview/diff) since the "As-built (read-only)" banner must
  // stay specific to an actually-archived fork, not a temporary history view
  // of an otherwise-editable one.
  const isArchivedFork = isLiveEdit && fork?.status === "ARCHIVED";

  const {
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    addEnrichedEdge,
    addEnrichedEdges,
    addDeviceNode,
    removeEdgesIncidentToNodes,
    selectedEdgeLayer,
    setSelectedEdgeLayer,
    updateEdgePathStatuses,
    clearTopology,
    loadCanvas,
    acceptProposalNodes,
    rejectProposalNodes,
  } = useTopologyStore();

  // Placeholders are excluded from every persistence path (parent topology
  // save, fork save, fork autosave): they are not devices or wiring, only a
  // reserve-time planning aid. Edges touching one are refused at draw time;
  // the edge filter here is belt and braces.
  // Stripping is memoized separately, keyed on [edges] alone (issue #517
  // review round 3 item 12.6): it does not depend on nodes or
  // selectedEdgeLayer at all, so recomputing it whenever THOSE change (as a
  // single combined memo below would) is wasted work.
  const strippedEdges = useMemo(() => edges.map(stripTransientEdgeFields), [edges]);

  // The live draft canvas as it would be persisted right now. Computed early
  // (ahead of the read-only/render wiring below) because useForkVersionPreview
  // needs it as the "current draft" side of a diff and as the snapshot
  // restored when a history view exits.
  const persistableCanvas = useMemo<CanvasData>(() => {
    const placeholderIds = new Set(nodes.filter(isDynamicPlaceholder).map((n) => n.id));
    return {
      nodes: placeholderIds.size === 0 ? nodes : nodes.filter((n) => !placeholderIds.has(n.id)),
      edges:
        placeholderIds.size === 0
          ? strippedEdges
          : strippedEdges.filter((e) => !placeholderIds.has(e.source) && !placeholderIds.has(e.target)),
      selectedEdgeLayer,
    };
  }, [nodes, strippedEdges, selectedEdgeLayer]);

  // Indirection for autosave.flush (issue #622 review): useForkVersionPreview
  // needs a flush callback to call before it hijacks the canvas store, but
  // useForkAutosave's own `enabled` depends on isReadOnly, which depends on
  // forkPreview.isActive below: autosave can only be constructed AFTER
  // forkPreview. A ref breaks the cycle: forkPreview gets a stable wrapper
  // now, autosave is constructed later, and an effect keeps the ref pointed
  // at the current flush.
  const flushAutosaveRef = useRef<() => void>(() => {});
  const flushAutosave = useCallback(() => flushAutosaveRef.current(), []);

  // Fork version preview/diff/restore state (issue #622, ADR 0006 addendum).
  // Inert outside live-edit mode (reservationId null keeps every query
  // disabled). Owns the temporary canvas-store swap for Preview/Diff so this
  // page only wires its result to the ReactFlow props and ForkHistoryPanel.
  const forkPreview = useForkVersionPreview({
    reservationId: isLiveEdit ? reservationId : null,
    currentCanvas: persistableCanvas,
    loadCanvas,
    flushAutosave,
  });
  const isHistoryViewActive = forkPreview.isActive;
  const historyViewBannerMode: "preview" | "diff" | null =
    forkPreview.mode === "preview" || forkPreview.mode === "diff" ? forkPreview.mode : null;

  // The union that actually locks the canvas: an archived fork's as-built
  // record, OR a history-view (preview/diff) currently painted over the live
  // draft. A history view must lock editing too, not just hide Save: it has
  // hijacked the store's nodes/edges (see useForkVersionPreview), so an edit
  // made while it is up would corrupt the overlay and, if it ever reached
  // autosave, the draft itself.
  const isReadOnly = isArchivedFork || isHistoryViewActive;

  const { data: aiStatus } = useAIStatus();

  // Resolve a stable map: React Flow node id to inventory device id. Edges store
  // node ids; pathfind needs device ids.
  const nodeIdToDeviceId = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of nodes) {
      const deviceId = (node.data as DeviceNodeData)?.device?.id;
      if (deviceId) map.set(node.id, deviceId);
    }
    return map;
  }, [nodes]);

  // Build the unique list of (source_device, target_device) pairs to validate.
  // Skips proposal edges; the user accepts them before they need a real check.
  const pathfindPairs = useMemo<DevicePair[]>(() => {
    const seen = new Set<string>();
    const pairs: DevicePair[] = [];
    for (const edge of edges) {
      if (isAnnotationEdge(edge.data)) continue;
      const src = nodeIdToDeviceId.get(edge.source);
      const tgt = nodeIdToDeviceId.get(edge.target);
      if (!src || !tgt) continue;
      const key = `${src}::${tgt}`;
      if (seen.has(key)) continue;
      seen.add(key);
      pairs.push({ sourceDeviceId: src, targetDeviceId: tgt });
    }
    return pairs;
  }, [edges, nodeIdToDeviceId]);

  const { data: pathfindResults } = usePathfindPairs(pathfindPairs);

  // Render-only view of the canvas edges (issue #517): two or more edges
  // sharing a device pair collapse into one bundledEdge with a count badge.
  // The underlying store (edges above) is untouched, still one object per
  // connection with its own id; only what React Flow paints changes.
  // bundleMembers maps a synthetic bundle id back to its real member edge
  // ids, so selection/Delete on a bundle can be expanded before it reaches
  // the store (review item 3: an unexpanded change against a bundle id is a
  // no-op on the store, an undeletable, unselectable bundle).
  const { renderEdges, bundleMembers } = useMemo(
    () => groupEdgesForRender(edges, isReadOnly),
    [edges, isReadOnly],
  );

  // A select or remove EdgeChange targeting a bundle id is expanded into the
  // same change against every one of its member ids before forwarding to
  // the store; changes targeting a real edge id (the common case, a single
  // unbundled connection) pass through unchanged.
  //
  // A 'replace' change is deliberately NOT expanded (issue #517 review round
  // 3 item 11): its `item` field is the edge React Flow wants written into
  // that slot, and for a bundle change that item IS THE SYNTHETIC BUNDLE
  // OBJECT (the `{ members: [...] }` shape from groupEdgesForRender), never
  // a real per-member edge. Expanding it would write that synthetic shape
  // into a real store edge's slot, corrupting it. Passed through untouched
  // instead: applyEdgeChanges looks the change up by id, finds no matching
  // store edge (bundle ids are synthetic and never appear in the store), and
  // no-ops harmlessly.
  const handleEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const expanded = changes.flatMap((change) => {
        if ("id" in change && bundleMembers.has(change.id)) {
          if (change.type === "select" || change.type === "remove") {
            return bundleMembers.get(change.id)!.map((id) => ({ ...change, id }));
          }
          return [change];
        }
        return [change];
      });
      onEdgesChange(expanded);
    },
    [onEdgesChange, bundleMembers],
  );

  // Belt-and-suspenders for node deletion (review item 3, re-examined in the
  // item-1 follow-up review once bundle.selected became state-faithful):
  // React Flow's own incident-edge computation for a deleted node runs
  // against the CURRENT `edges` prop, which is the bundled render view
  // here, so relying solely on it (even via handleEdgesChange above) to
  // reach every real member edge is not something to trust blindly. Kept
  // deliberately rather than removed: every vitest test in this codebase
  // (including this file's own) mocks React Flow itself out entirely, so
  // there is no test-level way to prove real React Flow's internal
  // deleteElements/incident-edge logic would still reach bundle members
  // correctly now that selection is faithful; that can only be live-gated
  // against a real browser. Empirically removing this handler and rerunning
  // the node-delete test does fail it (confirmed before restoring), which
  // at minimum proves the mocked harness itself has no other path to the
  // same guarantee. This explicit handler removes every store edge touching
  // a deleted node directly, independent of whatever React Flow itself
  // decided to emit, so the guarantee holds regardless of the answer to
  // that untestable question.
  const handleNodesDelete = useCallback(
    (deleted: Node[]) => {
      removeEdgesIncidentToNodes(deleted.map((n) => n.id));
    },
    [removeEdgesIncidentToNodes],
  );

  // Reconcile pathfind results back onto each edge so LayerEdge renders the
  // right color and label. Treats persisted pathValid as a stale cache: every
  // canvas load triggers a fresh resolution. Batched into one store commit
  // (issue #517 review item 10a): the previous per-edge updateEdgePathStatus
  // call was one set() (and one re-render) per changed edge on every
  // pathfind response, which scales badly with edge count.
  useEffect(() => {
    if (!pathfindResults) return;
    const updates = new Map<string, { pathValid: boolean | null; hopCount?: number }>();
    for (const edge of edges) {
      if (isAnnotationEdge(edge.data)) continue;
      const src = nodeIdToDeviceId.get(edge.source);
      const tgt = nodeIdToDeviceId.get(edge.target);
      if (!src || !tgt) continue;
      const result = pathfindResults.get(`${src}::${tgt}`);
      if (!result) continue;
      const reachable = result.reachable;
      const hops = result.hop_count;
      if (edge.data?.pathValid !== reachable || edge.data?.pathHopCount !== hops) {
        updates.set(edge.id, { pathValid: reachable, hopCount: hops });
      }
    }
    if (updates.size > 0) updateEdgePathStatuses(updates);
  }, [pathfindResults, edges, nodeIdToDeviceId, updateEdgePathStatuses]);

  // Disable the Reserve button when any committed edge is invalid. The
  // reservations service enforces the same rule server-side; this is UX
  // only. Uses resolveEdgeStroke's own isInvalid (issue #517 review round 3
  // item 12.7) instead of re-deriving the same pathValid/portsCabled
  // precedence inline, so this can never drift from what LayerEdge/
  // BundledEdge actually render as red.
  const invalidEdges = useMemo(
    () => edges.filter((e) => !isAnnotationEdge(e.data) && resolveEdgeStroke(e.data).isInvalid),
    [edges],
  );
  const hasInvalidEdges = invalidEdges.length > 0;

  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [showReserveModal, setShowReserveModal] = useState(false);
  const [showAIDialog, setShowAIDialog] = useState(false);
  const [showAICommit, setShowAICommit] = useState(false);
  const [pendingConnection, setPendingConnection] = useState<PendingConnection | null>(null);
  const [pendingElementAttach, setPendingElementAttach] = useState<PendingElementAttach | null>(
    null,
  );
  // Entry-point resolution (issue #517 addendum decision 1 left the concrete
  // trigger open): the full wiring dialog is the primary post-draw surface.
  // "Quick connect" is a toolbar toggle that, while on, opens the compact
  // popover instead for the next drawn line; the popover's own "Open wiring
  // dialog" link escalates a single in-flight connection back to the full
  // dialog without losing the device pair.
  const [quickConnectMode, setQuickConnectMode] = useState(false);

  // Cross-session duplicate prevention (issue #517 review item 8, scope
  // corrected in review round 3 item 5): a port already used by ANY
  // non-proposal canvas edge incident to a pending device is unavailable in
  // a fresh dialog session, regardless of who the OTHER end of that edge
  // is. A port used to wire the pending device to some THIRD device is just
  // as physically spoken-for as one already wired to the current
  // counterpart; scoping this to only edges between the exact pending pair
  // (the original implementation) missed that case entirely.
  const existingWiredPortIds = useMemo(() => {
    const sourcePortIds = new Set<string>();
    const targetPortIds = new Set<string>();
    if (!pendingConnection) return { sourcePortIds, targetPortIds };
    const { source: pendingSourceNode, target: pendingTargetNode } = pendingConnection.connection;
    for (const e of edges) {
      if (isAnnotationEdge(e.data)) continue;
      const srcPortId = e.data?.source_port_id;
      const tgtPortId = e.data?.target_port_id;
      if (e.source === pendingSourceNode && srcPortId) sourcePortIds.add(srcPortId);
      if (e.target === pendingSourceNode && tgtPortId) sourcePortIds.add(tgtPortId);
      if (e.source === pendingTargetNode && srcPortId) targetPortIds.add(srcPortId);
      if (e.target === pendingTargetNode && tgtPortId) targetPortIds.add(tgtPortId);
    }
    return { sourcePortIds, targetPortIds };
  }, [edges, pendingConnection]);

  // Same cross-session duplicate-prevention rule for ElementAttachDialog (ADR
  // 0012 "Editing surface", "identical to WiringDialog's rule"): a port
  // already wired on the canvas to ANY node, device or element, is
  // unavailable. Only the pending device's own node id is relevant here;
  // there is no counterpart-device side to also track.
  const existingWiredElementDevicePortIds = useMemo(() => {
    const portIds = new Set<string>();
    if (!pendingElementAttach) return portIds;
    const pendingDeviceNode = pendingElementAttach.connection.source === pendingElementAttach.elementNodeId
      ? pendingElementAttach.connection.target
      : pendingElementAttach.connection.source;
    for (const e of edges) {
      if (isAnnotationEdge(e.data)) continue;
      const srcPortId = e.data?.source_port_id;
      const tgtPortId = e.data?.target_port_id;
      if (e.source === pendingDeviceNode && srcPortId) portIds.add(srcPortId);
      if (e.target === pendingDeviceNode && tgtPortId) portIds.add(tgtPortId);
    }
    return portIds;
  }, [edges, pendingElementAttach]);

  const [pendingProposal, setPendingProposal] = useState<AIGenerateResponse | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [showSaveAsTemplate, setShowSaveAsTemplate] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [templateError, setTemplateError] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [previewVersion, setPreviewVersion] = useState<TopologyVersion | null>(null);
  const [preservedBeforePreview, setPreservedBeforePreview] = useState<CanvasData | null>(null);
  const [diffPair, setDiffPair] = useState<{ a: TopologyVersion; b: TopologyVersion } | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<TopologyVersion | null>(null);
  const [blockingReservations, setBlockingReservations] = useState<
    Array<{ id: string; status: string; end_time?: string }> | undefined
  >(undefined);
  const initializedRef = useRef(false);
  // Bumped on every preview request and on exit/restore, so a slow hydration
  // from a superseded or exited preview can never clobber the store after the
  // fact (issue #627).
  const previewRequestRef = useRef(0);
  const restoreVersion = useRestoreVersion(id);
  const createTemplate = useCreateTemplateFromTopology();

  // Load canvas data from backend when the source loads. Hydrate each node's
  // device from a fresh inventory fetch first, so the editor is resilient to
  // thin or stale persisted nodes (a deleted/missing device degrades to its
  // persisted data and is flagged by validation, never throwing). Loading the
  // persisted canvas first would flash blank/white nodes, so we await hydration
  // before the single loadCanvas call.
  //
  // In live-edit mode the source is the reservation's fork, not the parent
  // topology: the fork carries its own canvas (deep-copied from the parent at
  // creation, then diverging per save). forkLoaded gates the autosave so loading
  // the fork never counts as an edit.
  useEffect(() => {
    if (isLiveEdit) {
      if (fork && !initializedRef.current) {
        initializedRef.current = true;
        const persisted = fork.canvas_data;
        const applyLoad =
          persisted && persisted.nodes
            ? hydrateAndLoadCanvas(persisted, loadCanvas)
            : Promise.resolve().then(() => clearTopology());
        // Flip forkLoaded off the sync effect body (in a promise callback) so the
        // autosave baseline is seeded only once the loaded canvas is in the store.
        applyLoad.finally(() => setForkLoaded(true));
      }
      return;
    }
    if (topology && !initializedRef.current) {
      initializedRef.current = true;
      if (topology.canvas_data) {
        void hydrateAndLoadCanvas(topology.canvas_data, loadCanvas);
      } else {
        clearTopology();
      }
    }
  }, [isLiveEdit, fork, topology, loadCanvas, clearTopology]);

  // Debounced fork-draft autosave: PUTs the loose canvas a couple of seconds
  // after edits pause and flushes on unmount. Enabled only for an editable
  // (non-archived) fork that has finished loading.
  const autosave = useForkAutosave({
    reservationId: isLiveEdit ? reservationId : null,
    canvas: persistableCanvas,
    enabled: isLiveEdit && !isReadOnly && forkLoaded,
  });
  useEffect(() => {
    flushAutosaveRef.current = autosave.flush;
  }, [autosave.flush]);

  // Reset initialized ref when navigating to a different topology or reservation
  useEffect(() => {
    return () => {
      initializedRef.current = false;
      setForkLoaded(false);
    };
  }, [id, reservationId]);

  // Excludes both placeholders (no device id) and network elements (no
  // `data.device` at all; reading `.device.id` on one would throw, ADR 0012
  // "Canvas shape" site :489).
  const allDeviceIds = useMemo(
    () =>
      [
        ...new Set(
          nodes.filter(isDeviceNode).map((n) => (n.data as DeviceNodeData).device.id)
        ),
      ],
    [nodes]
  );

  // One entry per placeholder node: the reserve modal prefills its dynamic
  // requests from these (count expands into repeated {template_id} items).
  const dynamicPrefill = useMemo(
    () =>
      nodes.filter(isDynamicPlaceholder).map((n) => {
        const data = n.data as DynamicPlaceholderNodeData;
        return { templateId: data.templateId, count: data.count };
      }),
    [nodes]
  );

  // A true result no longer implies both endpoints are device nodes (ADR
  // 0012 "Attachments"): device-to-element is a valid connection too, and
  // skips the topology-type check below since it only makes sense between
  // two devices. Callers branching on the result (handleConnect) must still
  // check isNetworkElement themselves before reading either side's `.device`.
  const isValidConnection = useCallback(
    (connection: Connection | { source: string; target: string }): boolean => {
      const sourceNode = nodes.find((n) => n.id === connection.source);
      const targetNode = nodes.find((n) => n.id === connection.target);

      if (!sourceNode || !targetNode) return false;

      if (isDynamicPlaceholder(sourceNode) || isDynamicPlaceholder(targetNode)) {
        toast.error("Dynamic placeholders have no ports until the reservation activates", {
          id: "dynamic-placeholder",
        });
        return false;
      }

      // ADR 0012 "Attachments": element-to-element is refused, since neither
      // side has a device or a port. Device-to-element is the whole feature,
      // so it is valid here (and skips the topology-type check below, which
      // only makes sense between two devices) and branches in handleConnect
      // to open ElementAttachDialog instead of the wiring dialogs.
      if (isNetworkElement(sourceNode) && isNetworkElement(targetNode)) {
        toast.error("Network elements cannot be linked to each other", {
          id: "element-to-element",
        });
        return false;
      }
      if (isNetworkElement(sourceNode) || isNetworkElement(targetNode)) {
        return true;
      }

      const sourceType = (sourceNode.data as DeviceNodeData).device.topology_type;
      const targetType = (targetNode.data as DeviceNodeData).device.topology_type;

      if (sourceType !== targetType) {
        toast.error(
          `Cannot connect ${sourceType} and ${targetType} devices: topology types must match`,
          { id: "topology-mismatch" }
        );
        return false;
      }
      return true;
    },
    [nodes]
  );

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      if (isReadOnly) return;

      const dynamicJson = event.dataTransfer.getData("application/herd-dynamic-template");
      if (dynamicJson) {
        // Dynamic requests are fixed at reservation create time; a live
        // reservation's fork cannot grow new instances.
        if (isLiveEdit) {
          toast.error("Dynamic instances are set when the reservation is created", {
            id: "dynamic-live-edit",
          });
          return;
        }
        const template: { id: string; name: string; icon?: string | null } =
          JSON.parse(dynamicJson);

        // One placeholder per template; edit its count instead of stacking copies.
        if (dynamicPrefill.some((entry) => entry.templateId === template.id)) return;

        const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
        const placeholder: Node<DynamicPlaceholderNodeData> = {
          id: genId(),
          type: "dynamicPlaceholderNode",
          position,
          data: {
            templateId: template.id,
            templateName: template.name,
            templateIcon: template.icon ?? null,
            count: 1,
          },
        };
        addDeviceNode(placeholder);
        return;
      }

      const elementJson = event.dataTransfer.getData("application/herd-network-element");
      if (elementJson) {
        // Unlike the placeholder branch, multiple elements of the same type
        // are allowed (ADR 0012 "Editing surface"): a topology can carry two
        // distinct VLAN segments, so there is no "already on canvas" guard
        // here.
        const dragged: { element_type: NetworkElementType; label: string } = JSON.parse(elementJson);
        const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
        const node: Node<NetworkElementNodeData> = {
          id: genId(),
          type: "networkElementNode",
          position,
          data: {
            element: {
              id: genId(),
              element_type: dragged.element_type,
              label: dragged.label,
              attrs: {},
            },
          },
        };
        addDeviceNode(node);
        return;
      }

      const deviceJson = event.dataTransfer.getData("application/herd-device");
      if (!deviceJson) return;

      const device: Device = JSON.parse(deviceJson);

      // Prevent adding the same device twice
      if (allDeviceIds.includes(device.id)) return;

      const position = screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });

      const newNode: Node<DeviceNodeData> = {
        id: genId(),
        type: "deviceNode",
        position,
        data: {
          device,
          label: device.name,
          topologyType: device.topology_type,
        },
      };

      addDeviceNode(newNode);
    },
    [isReadOnly, isLiveEdit, addDeviceNode, screenToFlowPosition, allDeviceIds, dynamicPrefill]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleConnect: OnConnect = useCallback(
    (connection) => {
      if (isReadOnly) return;
      const sourceNode = nodes.find((n) => n.id === connection.source);
      const targetNode = nodes.find((n) => n.id === connection.target);
      if (!sourceNode || !targetNode) return;
      // isValidConnection already refuses these with a toast; guard again so a
      // placeholder can never reach the port-selection modal.
      if (isDynamicPlaceholder(sourceNode) || isDynamicPlaceholder(targetNode)) return;

      // ADR 0012 "Editing surface": device-to-element opens ElementAttachDialog.
      // isValidConnection already refuses element-to-element with a toast;
      // guard again here so it can never reach either modal.
      if (isNetworkElement(sourceNode) || isNetworkElement(targetNode)) {
        if (isNetworkElement(sourceNode) && isNetworkElement(targetNode)) return;
        const elementNode = isNetworkElement(sourceNode) ? sourceNode : targetNode;
        const deviceNode = isNetworkElement(sourceNode) ? targetNode : sourceNode;
        const element = (elementNode.data as NetworkElementNodeData).element;
        const device = (deviceNode.data as DeviceNodeData).device;
        setPendingElementAttach({
          connection,
          deviceId: device.id,
          deviceName: device.name,
          deviceTopologyType: device.topology_type,
          elementNodeId: elementNode.id,
          elementId: element.id,
          elementLabel: element.label,
          elementType: element.element_type,
        });
        return;
      }

      const sourceDevice = (sourceNode.data as DeviceNodeData).device;
      const targetDevice = (targetNode.data as DeviceNodeData).device;

      setPendingConnection({
        connection,
        sourceDeviceId: sourceDevice.id,
        sourceDeviceName: sourceDevice.name,
        sourceTopologyType: sourceDevice.topology_type,
        targetDeviceId: targetDevice.id,
        targetDeviceName: targetDevice.name,
        targetTopologyType: targetDevice.topology_type,
        surface: quickConnectMode ? "quick" : "dialog",
      });
    },
    [isReadOnly, nodes, quickConnectMode]
  );

  // Quick-connect popover confirm: one line, same shape as the AI proposal
  // path and the wiring dialog's per-line data. Routed through
  // addEnrichedEdges (the same bulk, always-appends path the wiring dialog
  // uses) rather than addEnrichedEdge/addEdge, whose connectionExists guard
  // silently refuses a second edge on identical source/target handles
  // (review item 5): re-wiring an already-connected device pair from the
  // popover would otherwise drop the second line with no error at all.
  const handleConnectionConfirm = useCallback(
    (data: LayerEdgeData) => {
      if (!pendingConnection) return;
      const conn = pendingConnection.connection;
      // pathValid starts unresolved on every layer; the pathfind-pairs effect
      // populates it once the query returns.
      const edgeData: LayerEdgeData = {
        ...data,
        pathValid: null,
      };
      addEnrichedEdges([{ connection: conn, data: edgeData }]);
      setPendingConnection(null);
    },
    [pendingConnection, addEnrichedEdges]
  );

  // Full wiring dialog confirm: every session line becomes its own enriched
  // edge, added to the canvas store in a single commit (never collapsed into
  // one stored object; see topologyStore.addEnrichedEdges).
  const handleWiringConfirm = useCallback(
    (connections: SessionConnection[]) => {
      if (!pendingConnection) return;
      const conn = pendingConnection.connection;
      addEnrichedEdges(
        connections.map((line) => ({
          connection: conn,
          data: {
            layer: line.layer,
            source_port_id: line.sourcePortId,
            source_port_name: line.sourcePortName,
            target_port_id: line.targetPortId,
            target_port_name: line.targetPortName,
            portsCabled: line.portsCabled,
            pathValid: null,
          } satisfies LayerEdgeData,
        })),
      );
      setPendingConnection(null);
    },
    [pendingConnection, addEnrichedEdges]
  );

  const handleConnectionCancel = useCallback(() => {
    setPendingConnection(null);
  }, []);

  const handleEscalateToWiringDialog = useCallback(() => {
    setPendingConnection((pc) => (pc ? { ...pc, surface: "dialog" } : pc));
  }, []);

  // ElementAttachDialog confirm: every selected port becomes its own
  // attachment edge, added in ONE addEnrichedEdges call (ADR 0012 "Editing
  // surface"). source_port_name is set, no target port (the element side has
  // no ports). The store's addEnrichedEdges normalizes direction so the
  // device always lands as source regardless of which side the drawn
  // connection started from.
  const handleElementAttachConfirm = useCallback(
    (selections: ElementAttachSelection[]) => {
      if (!pendingElementAttach) return;
      const conn = pendingElementAttach.connection;
      addEnrichedEdges(
        selections.map((sel) => ({
          connection: conn,
          data: {
            layer: selectedEdgeLayer,
            source_port_id: sel.portId,
            source_port_name: sel.portName,
            pathValid: null,
          } satisfies LayerEdgeData,
        })),
      );
      setPendingElementAttach(null);
    },
    [pendingElementAttach, addEnrichedEdges, selectedEdgeLayer],
  );

  const handleElementAttachCancel = useCallback(() => {
    setPendingElementAttach(null);
  }, []);

  const handleAIProposal = useCallback(
    (response: AIGenerateResponse) => {
      // Reject any stale proposal before rendering the new one
      rejectProposalNodes();

      const missing = response.devices.filter((d) => !d.device);
      if (missing.length > 0) {
        toast.error(
          `AI proposal is missing resolved devices for ${missing.length} role(s); discarding`
        );
        return;
      }

      // Skip any resolved device that is already on the canvas; the resolver
      // cannot see the canvas and could pick a device the user just dropped.
      const canvasDeviceIdSet = collectCanvasDeviceIds(nodes);
      const duplicates = response.devices.filter(
        (d) => d.device && canvasDeviceIdSet.has(d.device.id)
      );
      if (duplicates.length > 0) {
        toast.error(
          `AI picked ${duplicates.length} device(s) already on the canvas; proposal discarded`
        );
        return;
      }

      const roleToNodeId = new Map<string, string>();
      const baseX = 200;
      const baseY = 200;
      const stepX = 220;

      response.devices.forEach((proposed, idx) => {
        const resolved = proposed.device as Device;
        const nodeId = genId();
        const node: Node<DeviceNodeData> = {
          id: nodeId,
          type: "deviceNode",
          position: { x: baseX + idx * stepX, y: baseY },
          data: {
            device: resolved,
            label: resolved.name,
            topologyType: resolved.topology_type,
            isProposal: true,
          },
        };
        addDeviceNode(node);
        roleToNodeId.set(proposed.role, nodeId);
      });

      // Network elements (issue #632): one ghost networkElementNode per
      // proposed element, placed in a row below the devices. Role goes into
      // the SAME roleToNodeId map as devices (D1: roles are unique across
      // both), so the edge loop below needs no element-specific branch; a
      // dangling role (e.g. a rejected element_to_element pair that somehow
      // slipped through) is simply skipped like any other unresolved role.
      const elementRowY = baseY + stepX;
      (response.elements ?? []).forEach((proposed, idx) => {
        const nodeId = genId();
        const node: Node<NetworkElementNodeData> = {
          id: nodeId,
          type: "networkElementNode",
          position: { x: baseX + idx * stepX, y: elementRowY },
          data: {
            element: {
              id: genId(),
              element_type: proposed.element_type as NetworkElementType,
              label: proposed.label,
              attrs: proposed.attrs ?? {},
            },
            isProposal: true,
          },
        };
        addDeviceNode(node);
        roleToNodeId.set(proposed.role, nodeId);
      });

      response.edges.forEach((edge) => {
        const sourceId = roleToNodeId.get(edge.source_role);
        const targetId = roleToNodeId.get(edge.target_role);
        if (!sourceId || !targetId) return;
        // No port fields here even for a device-to-element edge: the
        // committer picks the device-side port on accept (D2), and the
        // store's addEnrichedEdge normalizes direction so the device lands
        // as source regardless of which role came first in the response.
        addEnrichedEdge(
          { source: sourceId, target: targetId, sourceHandle: null, targetHandle: null },
          { layer: edge.layer as EdgeLayerType, isProposal: true }
        );
      });

      setPendingProposal(response);
    },
    [addDeviceNode, addEnrichedEdge, nodes, rejectProposalNodes]
  );

  const handleProposalAccept = useCallback(() => {
    setShowAICommit(true);
  }, []);

  const handleProposalModify = useCallback(() => {
    // Keep the ghost nodes/edges visible so the user can edit them manually.
    // Accepting them becomes an implicit part of saving the topology.
    acceptProposalNodes();
    setPendingProposal(null);
    toast("Proposal accepted for editing");
  }, [acceptProposalNodes]);

  const handleProposalReject = useCallback(() => {
    rejectProposalNodes();
    setPendingProposal(null);
  }, [rejectProposalNodes]);

  const handleSave = async () => {
    if (!id) return;
    const trimmed = description.trim();
    try {
      await updateTopology.mutateAsync({
        id,
        // Dynamic placeholders never persist into the parent topology.
        canvas_data: persistableCanvas,
        ...(trimmed ? { description: trimmed } : {}),
      });
    } catch {
      // The mutation's onError already surfaced the failure as a toast.
      // Swallow here so the success path below does not run and the
      // rejection does not dangle as an unhandled promise.
      return;
    }
    setDescription("");
    if (dynamicPrefill.length > 0) {
      // Owner's call on #472: placeholders are ephemeral, but a save must not
      // silently drop planning work on the next reload.
      toast.success("Topology saved. Dynamic placeholders are not saved; reserve to keep them");
    } else {
      toast.success("Topology saved");
    }
  };

  // Live-edit commit (ADR 0006 Decision 6): reconcile the reservation's FORK
  // against the canvas, then re-point the reservation's device set. The fork save
  // replaces the old parent-topology PUT: it appends a fork version and leaves the
  // parent's TopologyVersion history byte-for-byte unchanged. The device PATCH is
  // unchanged and is still what triggers incremental provisioning
  // (reservation.updated: connect/disconnect ports, add/remove from VLAN).
  // Blocked when any edge is unreachable; the backend enforces the same rule.
  const handleCommitToReservation = useCallback(async () => {
    if (!reservationId) return;
    if (hasInvalidEdges) {
      toast.error("Fix unreachable edges before committing");
      return;
    }
    setIsCommitting(true);
    try {
      const result = await saveFork.mutateAsync({
        reservationId,
        canvasData: persistableCanvas,
      });
      // The reconcile captured this canvas, so cancel any pending draft flush.
      autosave.markClean();
      toast.custom((t) => (
        <ForkSaveResultToast result={result} onDismiss={() => toast.dismiss(t.id)} />
      ));
      // The fork version is already committed at this point; a device-set PATCH
      // failure must not be reported as a failed save, it gets its own message.
      try {
        await updateReservation.mutateAsync({
          id: reservationId,
          data: { device_ids: allDeviceIds },
        });
      } catch {
        toast.error(
          "Fork saved, but updating the reservation's device set failed; commit again to retry",
        );
      }
    } catch (err) {
      // A structured 409 (cross-reservation port claim) opens the conflict dialog
      // and keeps the drawing so the user can rework it. Any other error (a plain
      // 409 for a non-ACTIVE or ARCHIVED fork, a transport failure) toasts.
      const conflict = forkConflictDetail(err);
      if (conflict) {
        setSaveConflict(conflict);
      } else {
        const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data
          ?.detail;
        const message =
          typeof detail === "string"
            ? detail
            : (detail as { message?: string } | undefined)?.message ??
              "Failed to save the reservation fork";
        toast.error(message);
      }
    } finally {
      setIsCommitting(false);
    }
  }, [
    reservationId,
    hasInvalidEdges,
    saveFork,
    autosave,
    persistableCanvas,
    updateReservation,
    allDeviceIds,
  ]);

  const handleCancelLiveEdit = useCallback(() => {
    navigate("/reservations");
  }, [navigate]);

  const handlePreviewVersion = useCallback(
    async (version: TopologyVersion) => {
      if (!id) return;
      // Invalidate any hydration still in flight for a prior preview request,
      // so an earlier version's slow fetch can never win a race against this
      // one and clobber the canvas it just finished loading.
      const token = ++previewRequestRef.current;
      if (!preservedBeforePreview) {
        setPreservedBeforePreview({ nodes, edges, selectedEdgeLayer });
      }
      try {
        const resp = await apiClient.get<TopologyVersionDetail>(
          `/cabling/topologies/${id}/versions/${version.id}`,
        );
        const canvas = resp.data.canvas_data;
        if (!canvas) {
          toast.error("Version has no canvas data");
          return;
        }
        const ghostCanvas: CanvasData = {
          ...canvas,
          nodes: canvas.nodes.map((n) => ({
            ...n,
            data: { ...(n.data as DeviceNodeData), isProposal: true },
          })),
          edges: canvas.edges.map((e) => ({
            ...e,
            data: { ...((e.data as LayerEdgeData | undefined) ?? { layer: "L1" }), isProposal: true },
          })),
        };
        // Route through hydrateAndLoadCanvas (issue #627), not a raw
        // loadCanvas: a version's canvas_data comes straight off the server
        // and can carry thin nodes (`{ device: { id } }` with no name or
        // topology_type), exactly like the fork-history preview this
        // mirrors. Hydration is async, so guard the eventual loadCanvas (and
        // the previewVersion flip below) with the token: if this request has
        // since been superseded or exited, its result must be dropped.
        await hydrateAndLoadCanvas(ghostCanvas, (hydrated) => {
          if (previewRequestRef.current === token) loadCanvas(hydrated);
        });
        if (previewRequestRef.current === token) setPreviewVersion(version);
      } catch {
        toast.error("Failed to load version");
      }
    },
    [id, nodes, edges, selectedEdgeLayer, preservedBeforePreview, loadCanvas],
  );

  const handleExitPreview = useCallback(() => {
    // Invalidate any preview hydration still in flight before restoring the
    // preserved canvas, so a late result cannot land after exit (issue #627).
    previewRequestRef.current += 1;
    if (preservedBeforePreview) {
      loadCanvas(preservedBeforePreview);
    }
    setPreservedBeforePreview(null);
    setPreviewVersion(null);
  }, [preservedBeforePreview, loadCanvas]);

  const handleRestoreConfirm = useCallback(
    async ({ description: desc, restoreName }: { description: string; restoreName: boolean }) => {
      if (!restoreTarget || !id) return;
      try {
        const updated = await restoreVersion.mutateAsync({
          versionId: restoreTarget.id,
          body: {
            ...(desc.trim() ? { description: desc.trim() } : {}),
            restore_name: restoreName,
          },
        });
        // Invalidate any preview hydration still in flight before loading the
        // restored canvas, so a late preview result cannot land after it
        // (issue #627).
        previewRequestRef.current += 1;
        if (updated.canvas_data) {
          loadCanvas(updated.canvas_data);
        }
        setRestoreTarget(null);
        setBlockingReservations(undefined);
        setPreservedBeforePreview(null);
        setPreviewVersion(null);
        toast.success(`Restored v${restoreTarget.version_number}`);
      } catch (err) {
        const response = (err as { response?: { status?: number; data?: { detail?: unknown } } })
          .response;
        if (response?.status === 409) {
          const detail = response.data?.detail as
            | { reservations?: Array<{ id: string; status: string; end_time?: string }> }
            | undefined;
          setBlockingReservations(detail?.reservations ?? []);
        } else {
          toast.error("Restore failed");
        }
      }
    },
    [restoreTarget, id, restoreVersion, loadCanvas],
  );

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-sm text-gray-400">Loading topology...</p>
      </div>
    );
  }

  return (
    <div className="flex h-full overflow-hidden">
      {/* Main canvas area (full width) */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Toolbar */}
        <div className="flex items-center gap-3 px-4 py-2 bg-white border-b border-gray-200">
          <button
            onClick={() => navigate("/topology")}
            className="text-sm text-gray-500 hover:text-gray-700 px-2 py-1 rounded hover:bg-gray-100"
          >
            Back
          </button>
          <span className="text-sm font-medium text-gray-900 truncate">
            {topology?.name ?? "Topology"}
          </span>
          {isLiveEdit && !isArchivedFork && (
            <span className="text-xs font-medium px-2 py-0.5 rounded bg-blue-100 text-blue-700">
              Editing reservation{liveReservation ? ` (${liveReservation.purpose})` : ""}
            </span>
          )}
          {isArchivedFork && (
            <span className="text-xs font-medium px-2 py-0.5 rounded bg-gray-200 text-gray-700">
              As-built (read-only){liveReservation ? ` (${liveReservation.purpose})` : ""}
            </span>
          )}
          <div className="w-px h-5 bg-gray-300" />
          <span className="text-sm font-medium text-gray-600">Edge layer:</span>
          <div className="flex gap-1">
            {LAYER_OPTIONS.map((layer) => (
              <button
                key={layer}
                onClick={() => setSelectedEdgeLayer(layer)}
                title={LAYER_DESCRIPTIONS[layer]}
                aria-label={`${layer}: ${LAYER_DESCRIPTIONS[layer]}`}
                aria-pressed={selectedEdgeLayer === layer}
                className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
                  selectedEdgeLayer === layer
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                }`}
              >
                {layer}
              </button>
            ))}
          </div>
          <div className="w-px h-5 bg-gray-300" />
          <button
            onClick={() => setQuickConnectMode((v) => !v)}
            aria-pressed={quickConnectMode}
            title="When on, drawing a line opens the compact quick-connect popover instead of the full wiring dialog"
            className={`text-sm px-3 py-1 rounded font-medium transition-colors ${
              quickConnectMode
                ? "bg-blue-600 text-white"
                : "bg-gray-100 text-gray-700 hover:bg-gray-200"
            }`}
          >
            Quick connect
          </button>
          <div className="ml-auto flex items-center gap-2">
            {aiStatus?.enabled && (
              <button
                onClick={() => setShowAIDialog(true)}
                className="text-sm text-purple-600 hover:text-purple-800 px-2 py-1 rounded hover:bg-purple-50"
              >
                Use AI
              </button>
            )}
            {!isLiveEdit && (
              <button
                onClick={() => setShowReserveModal(true)}
                disabled={(allDeviceIds.length === 0 && dynamicPrefill.length === 0) || hasInvalidEdges}
                title={
                  hasInvalidEdges
                    ? `Cannot reserve: ${invalidEdges.length} edge${invalidEdges.length !== 1 ? "s" : ""} have no physical path or use uncabled ports`
                    : undefined
                }
                className="text-sm text-blue-600 hover:text-blue-800 px-2 py-1 rounded hover:bg-blue-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Reserve Topology ({allDeviceIds.length} device{allDeviceIds.length !== 1 ? "s" : ""})
              </button>
            )}
            {!isLiveEdit && (
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe this change (optional)"
                aria-label="Version description"
                disabled={!!previewVersion}
                className="text-sm border border-gray-300 rounded px-2 py-1 w-56 focus:outline-none focus:border-blue-500 disabled:bg-gray-100"
              />
            )}
            {!isLiveEdit && (
              <button
                onClick={handleSave}
                disabled={updateTopology.isPending || !!previewVersion}
                className="text-sm text-green-600 hover:text-green-800 px-2 py-1 rounded hover:bg-green-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {updateTopology.isPending ? "Saving..." : "Save"}
              </button>
            )}
            <button
              onClick={() => setShowHistory((v) => !v)}
              className="text-sm text-gray-700 hover:text-gray-900 px-2 py-1 rounded hover:bg-gray-100"
            >
              History
            </button>
            {!isLiveEdit && (
              <button
                onClick={() => setShowSaveAsTemplate(true)}
                disabled={!!previewVersion}
                className="text-sm text-gray-700 hover:text-gray-900 px-2 py-1 rounded hover:bg-gray-100 disabled:opacity-50"
              >
                Save as Template
              </button>
            )}
            {previewVersion && (
              <button
                onClick={handleExitPreview}
                className="text-sm text-purple-700 bg-purple-100 hover:bg-purple-200 px-2 py-1 rounded"
              >
                Exit preview (v{previewVersion.version_number})
              </button>
            )}
            <button
              onClick={() => setShowClearConfirm(true)}
              disabled={!!previewVersion || isReadOnly}
              className="text-sm text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Clear canvas
            </button>
          </div>
        </div>

        {/* React Flow canvas */}
        <div className="flex-1 relative">
          {/* Floating Equipment Browser */}
          <FloatingPanel title="Equipment" defaultPosition={{ x: 16, y: 16 }}>
            <EquipmentBrowser canvasDeviceIds={allDeviceIds} />
          </FloatingPanel>

          {pendingProposal && (
            <AIProposalBar
              purpose={pendingProposal.purpose}
              deviceCount={pendingProposal.devices.length}
              edgeCount={pendingProposal.edges.length}
              notes={pendingProposal.notes}
              onAccept={handleProposalAccept}
              onModify={handleProposalModify}
              onReject={handleProposalReject}
            />
          )}

          {isLiveEdit && !isReadOnly && !pendingProposal && (
            <LiveEditBar
              deviceCount={allDeviceIds.length}
              invalidEdgeCount={invalidEdges.length}
              isCommitting={isCommitting}
              autosaveStatus={autosave.status}
              onCommit={handleCommitToReservation}
              onCancel={handleCancelLiveEdit}
            />
          )}

          {isArchivedFork && !pendingProposal && (
            <AsBuiltBar deviceCount={allDeviceIds.length} onClose={handleCancelLiveEdit} />
          )}

          {historyViewBannerMode && !pendingProposal && (
            <ForkVersionPreviewBar
              mode={historyViewBannerMode}
              previewVersion={forkPreview.previewVersion}
              diffBase={forkPreview.diffBase}
              diffCompareLabel={forkPreview.diffCompareLabel}
              onExit={forkPreview.exit}
            />
          )}

          <ReactFlow
            onDrop={onDrop}
            onDragOver={onDragOver}
            nodes={nodes}
            edges={renderEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={handleEdgesChange}
            onNodesDelete={handleNodesDelete}
            onConnect={handleConnect}
            isValidConnection={isValidConnection}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            connectionMode={ConnectionMode.Loose}
            nodesDraggable={!isReadOnly}
            nodesConnectable={!isReadOnly}
            elementsSelectable={!isReadOnly}
            fitView
            deleteKeyCode={isReadOnly ? null : "Delete"}
          >
            <Background gap={16} size={1} color="#e5e7eb" />
            <Controls />
            <MiniMap
              nodeColor={(node) => {
                if (node.type === "dynamicPlaceholderNode") return "#a855f7";
                if (node.type === "networkElementNode") return "#9ca3af";
                const data = node.data as DeviceNodeData;
                return data?.device?.topology_type === "CLOUD" ? "#a855f7" : "#3b82f6";
              }}
            />
          </ReactFlow>
        </div>

        <ConfirmDialog
          open={showClearConfirm}
          title="Clear canvas?"
          description="This will remove all devices and connections from the canvas."
          confirmLabel="Clear"
          destructive
          onConfirm={() => {
            clearTopology();
            setShowClearConfirm(false);
          }}
          onCancel={() => setShowClearConfirm(false)}
        />

        {showReserveModal && (
          <CreateReservationModal
            open={showReserveModal}
            deviceIds={allDeviceIds}
            topologyId={id}
            initialDynamicEntries={dynamicPrefill}
            onClose={() => setShowReserveModal(false)}
          />
        )}

        <AIDialog
          open={showAIDialog}
          onClose={() => setShowAIDialog(false)}
          onProposal={handleAIProposal}
        />

        <AICommitDialog
          open={showAICommit}
          proposal={pendingProposal}
          onClose={() => setShowAICommit(false)}
          onCommitted={(result) => {
            setPendingProposal(null);
            rejectProposalNodes();
            navigate(`/topology/${result.topology_id}`);
          }}
        />

        {pendingConnection && pendingConnection.surface === "quick" && (
          <QuickConnectPopover
            open={!!pendingConnection}
            sourceDeviceId={pendingConnection.sourceDeviceId}
            sourceDeviceName={pendingConnection.sourceDeviceName}
            targetDeviceId={pendingConnection.targetDeviceId}
            targetDeviceName={pendingConnection.targetDeviceName}
            defaultLayer={selectedEdgeLayer}
            existingWiredSourcePortIds={existingWiredPortIds.sourcePortIds}
            existingWiredTargetPortIds={existingWiredPortIds.targetPortIds}
            onConfirm={handleConnectionConfirm}
            onCancel={handleConnectionCancel}
            onEscalate={handleEscalateToWiringDialog}
          />
        )}

        {pendingConnection && pendingConnection.surface === "dialog" && (
          <WiringDialog
            open={!!pendingConnection}
            sourceDeviceId={pendingConnection.sourceDeviceId}
            sourceDeviceName={pendingConnection.sourceDeviceName}
            sourceTopologyType={pendingConnection.sourceTopologyType}
            targetDeviceId={pendingConnection.targetDeviceId}
            targetDeviceName={pendingConnection.targetDeviceName}
            targetTopologyType={pendingConnection.targetTopologyType}
            defaultLayer={selectedEdgeLayer}
            existingWiredSourcePortIds={existingWiredPortIds.sourcePortIds}
            existingWiredTargetPortIds={existingWiredPortIds.targetPortIds}
            onConfirm={handleWiringConfirm}
            onCancel={handleConnectionCancel}
          />
        )}

        {pendingElementAttach && (
          <ElementAttachDialog
            open={!!pendingElementAttach}
            deviceId={pendingElementAttach.deviceId}
            deviceName={pendingElementAttach.deviceName}
            deviceTopologyType={pendingElementAttach.deviceTopologyType}
            elementLabel={pendingElementAttach.elementLabel}
            elementType={pendingElementAttach.elementType}
            existingWiredPortIds={existingWiredElementDevicePortIds}
            onConfirm={handleElementAttachConfirm}
            onCancel={handleElementAttachCancel}
          />
        )}

        {/* In live-edit mode History lists the FORK's versions (ADR 0006), not the
            parent topology's, so an owner's edits never appear in the master's
            history. Preview/diff/restore (issue #622, ADR 0006 addendum): Restore
            renders ACTIVE-only, mirroring the Retry button's rule. */}
        {showHistory && isLiveEdit && (
          <ForkHistoryPanel
            versions={fork?.versions ?? []}
            isActiveReservation={liveReservation?.status === "ACTIVE"}
            draftRestoredFromId={fork?.draft_restored_from_id ?? null}
            preview={forkPreview}
            onClose={() => {
              // Closing the panel is the only other way out besides the
              // banner's own Exit button; without also exiting here, a
              // preview/diff left active keeps the canvas locked with the
              // panel gone and no visible way back in.
              forkPreview.exit();
              setShowHistory(false);
            }}
          />
        )}

        {showHistory && !isLiveEdit && id && (
          <HistoryPanel
            topologyId={id}
            onClose={() => setShowHistory(false)}
            onPreview={handlePreviewVersion}
            onRestore={(v) => {
              setRestoreTarget(v);
              setBlockingReservations(undefined);
            }}
            onCompare={(a, b) => setDiffPair({ a, b })}
            previewVersionId={previewVersion?.id ?? null}
          />
        )}

        <ForkConflictDialog
          open={!!saveConflict}
          detail={saveConflict}
          onClose={() => setSaveConflict(null)}
        />

        {diffPair && id && (
          <VersionDiffDialog
            open={!!diffPair}
            topologyId={id}
            versionA={diffPair.a}
            versionB={diffPair.b}
            onClose={() => setDiffPair(null)}
          />
        )}

        <RestoreConfirmDialog
          open={!!restoreTarget}
          version={restoreTarget}
          onClose={() => {
            setRestoreTarget(null);
            setBlockingReservations(undefined);
          }}
          onConfirm={handleRestoreConfirm}
          isPending={restoreVersion.isPending}
          blockingReservations={blockingReservations}
        />

        <Modal
          open={showSaveAsTemplate}
          onClose={() => {
            setShowSaveAsTemplate(false);
            setTemplateName("");
            setTemplateError(null);
          }}
          title="Save topology as template"
        >
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (!id || !templateName.trim()) return;
              setTemplateError(null);
              try {
                await createTemplate.mutateAsync({
                  topologyId: id,
                  name: templateName.trim(),
                });
                setShowSaveAsTemplate(false);
                setTemplateName("");
              } catch (err: unknown) {
                const detail = (err as { response?: { data?: { detail?: string } } })?.response
                  ?.data?.detail;
                setTemplateError(detail ?? "Failed to save template");
              }
            }}
          >
            <label
              htmlFor="template-name"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Template name
            </label>
            <input
              id="template-name"
              type="text"
              value={templateName}
              onChange={(e) => setTemplateName(e.target.value)}
              autoFocus
              className="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Standard 2-spine 4-leaf"
            />
            <p className="text-xs text-gray-500 mt-2">
              Devices in the canvas will be replaced with role placeholders. Roles are auto-named
              from each device's template.
            </p>
            {templateError && (
              <p className="text-xs text-red-600 mt-2">{templateError}</p>
            )}
            <div className="flex justify-end gap-2 mt-4">
              <button
                type="button"
                onClick={() => {
                  setShowSaveAsTemplate(false);
                  setTemplateName("");
                  setTemplateError(null);
                }}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!templateName.trim() || createTemplate.isPending}
                className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
              >
                {createTemplate.isPending ? "Saving..." : "Save"}
              </button>
            </div>
          </form>
        </Modal>
      </div>
    </div>
  );
}

export function TopologyEditorPage() {
  return (
    <ReactFlowProvider>
      <TopologyEditorInner />
    </ReactFlowProvider>
  );
}
