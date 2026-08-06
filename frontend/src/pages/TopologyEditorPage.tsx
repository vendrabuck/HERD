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
import { usePathfindPairs, type DevicePair } from "@/api/connections";
import { useAIStatus } from "@/api/ai";
import { hydrateCanvasNodes } from "@/api/inventory";
import { useTopologyStore } from "@/stores/topologyStore";
import { useForkAutosave } from "@/hooks/useForkAutosave";
import { EquipmentBrowser } from "@/components/equipment-browser/EquipmentBrowser";
import { FloatingPanel } from "@/components/ui/FloatingPanel";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";
import { ConnectionModal } from "@/components/topology-editor/ConnectionModal";
import { AIDialog } from "@/components/topology-editor/AIDialog";
import { AICommitDialog } from "@/components/topology-editor/AICommitDialog";
import { AIProposalBar } from "@/components/topology-editor/AIProposalBar";
import { LiveEditBar } from "@/components/topology-editor/LiveEditBar";
import { AsBuiltBar } from "@/components/topology-editor/AsBuiltBar";
import { ForkSaveResultToast } from "@/components/topology-editor/ForkSaveResultToast";
import { ForkConflictDialog } from "@/components/topology-editor/ForkConflictDialog";
import { ForkHistoryPanel } from "@/components/topology-editor/ForkHistoryPanel";
import { HistoryPanel } from "@/components/topology-editor/HistoryPanel";
import { VersionDiffDialog } from "@/components/topology-editor/VersionDiffDialog";
import { RestoreConfirmDialog } from "@/components/topology-editor/RestoreConfirmDialog";
import { Modal } from "@/components/ui/Modal";
import { useCreateTemplateFromTopology } from "@/api/topologyTemplates";
import apiClient from "@/api/client";
import { DeviceNode } from "@/components/topology-editor/nodes/DeviceNode";
import { DynamicPlaceholderNode } from "@/components/topology-editor/nodes/DynamicPlaceholderNode";
import { LayerEdge } from "@/components/topology-editor/edges/LayerEdge";
import type { Device } from "@/types/device.types";
import type { AIGenerateResponse } from "@/types/ai.types";
import type { ForkConflictDetail } from "@/types/reservation.types";
import type {
  CanvasData,
  CanvasNodeData,
  DeviceNodeData,
  DynamicPlaceholderNodeData,
  EdgeLayerType,
  LayerEdgeData,
  TopologyVersion,
  TopologyVersionDetail,
} from "@/types/topology.types";

interface PendingConnection {
  connection: Connection;
  sourceDeviceId: string;
  sourceDeviceName: string;
  targetDeviceId: string;
  targetDeviceName: string;
}

const nodeTypes = { deviceNode: DeviceNode, dynamicPlaceholderNode: DynamicPlaceholderNode };
const edgeTypes = { layerEdge: LayerEdge };

// Placeholder nodes are canvas-local planning artifacts: no inventory device
// id, no cabling, never persisted as devices or wiring.
const isDynamicPlaceholder = (node: Node<CanvasNodeData>) =>
  node.type === "dynamicPlaceholderNode";

const LAYER_OPTIONS: EdgeLayerType[] = ["L1", "L2", "L3"];

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
  // archived by the teardown paths), so mutations key off it directly.
  const isReadOnly = isLiveEdit && fork?.status === "ARCHIVED";

  const {
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    addEnrichedEdge,
    addDeviceNode,
    selectedEdgeLayer,
    setSelectedEdgeLayer,
    updateEdgePathStatus,
    clearTopology,
    loadCanvas,
    acceptProposalNodes,
    rejectProposalNodes,
  } = useTopologyStore();

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
      if (edge.data?.isProposal) continue;
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

  // Reconcile pathfind results back onto each edge so LayerEdge renders the
  // right color and label. Treats persisted pathValid as a stale cache: every
  // canvas load triggers a fresh resolution.
  useEffect(() => {
    if (!pathfindResults) return;
    for (const edge of edges) {
      if (edge.data?.isProposal) continue;
      const src = nodeIdToDeviceId.get(edge.source);
      const tgt = nodeIdToDeviceId.get(edge.target);
      if (!src || !tgt) continue;
      const result = pathfindResults.get(`${src}::${tgt}`);
      if (!result) continue;
      const reachable = result.reachable;
      const hops = result.hop_count;
      if (edge.data?.pathValid !== reachable || edge.data?.pathHopCount !== hops) {
        updateEdgePathStatus(edge.id, reachable, hops);
      }
    }
  }, [pathfindResults, edges, nodeIdToDeviceId, updateEdgePathStatus]);

  // Disable the Reserve button when any committed edge is invalid. The
  // reservations service enforces the same rule server-side; this is UX only.
  const invalidEdges = useMemo(
    () =>
      edges.filter(
        (e) =>
          !e.data?.isProposal &&
          (e.data?.pathValid === false || e.data?.portsCabled === false),
      ),
    [edges],
  );
  const hasInvalidEdges = invalidEdges.length > 0;

  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [showReserveModal, setShowReserveModal] = useState(false);
  const [showAIDialog, setShowAIDialog] = useState(false);
  const [showAICommit, setShowAICommit] = useState(false);
  const [pendingConnection, setPendingConnection] = useState<PendingConnection | null>(null);
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
            ? hydrateCanvasNodes(persisted)
                .then((hydrated) => loadCanvas(hydrated))
                .catch(() => loadCanvas(persisted))
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
        const persisted = topology.canvas_data;
        hydrateCanvasNodes(persisted)
          .then((hydrated) => loadCanvas(hydrated))
          // hydrateCanvasNodes already swallows per-device failures; this guards
          // a total fetch outage so the editor still shows the persisted canvas.
          .catch(() => loadCanvas(persisted));
      } else {
        clearTopology();
      }
    }
  }, [isLiveEdit, fork, topology, loadCanvas, clearTopology]);

  // Placeholders are excluded from every persistence path (parent topology
  // save, fork save, fork autosave): they are not devices or wiring, only a
  // reserve-time planning aid. Edges touching one are refused at draw time;
  // the edge filter here is belt and braces.
  const persistableCanvas = useMemo<CanvasData>(() => {
    const placeholderIds = new Set(nodes.filter(isDynamicPlaceholder).map((n) => n.id));
    if (placeholderIds.size === 0) return { nodes, edges, selectedEdgeLayer };
    return {
      nodes: nodes.filter((n) => !placeholderIds.has(n.id)),
      edges: edges.filter((e) => !placeholderIds.has(e.source) && !placeholderIds.has(e.target)),
      selectedEdgeLayer,
    };
  }, [nodes, edges, selectedEdgeLayer]);

  // Debounced fork-draft autosave: PUTs the loose canvas a couple of seconds
  // after edits pause and flushes on unmount. Enabled only for an editable
  // (non-archived) fork that has finished loading.
  const autosave = useForkAutosave({
    reservationId: isLiveEdit ? reservationId : null,
    canvas: persistableCanvas,
    enabled: isLiveEdit && !isReadOnly && forkLoaded,
  });

  // Reset initialized ref when navigating to a different topology or reservation
  useEffect(() => {
    return () => {
      initializedRef.current = false;
      setForkLoaded(false);
    };
  }, [id, reservationId]);

  const allDeviceIds = useMemo(
    () =>
      [
        ...new Set(
          nodes
            .filter((n) => !isDynamicPlaceholder(n))
            .map((n) => (n.data as DeviceNodeData).device.id)
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
          id:
            self.crypto?.randomUUID?.() ??
            `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`,
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
        id: self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`,
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

      const sourceDevice = (sourceNode.data as DeviceNodeData).device;
      const targetDevice = (targetNode.data as DeviceNodeData).device;

      setPendingConnection({
        connection,
        sourceDeviceId: sourceDevice.id,
        sourceDeviceName: sourceDevice.name,
        targetDeviceId: targetDevice.id,
        targetDeviceName: targetDevice.name,
      });
    },
    [isReadOnly, nodes]
  );

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
      addEnrichedEdge(conn, edgeData);
      setPendingConnection(null);
    },
    [pendingConnection, addEnrichedEdge]
  );

  const handleConnectionCancel = useCallback(() => {
    setPendingConnection(null);
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
      const canvasDeviceIdSet = new Set(
        nodes
          .filter((n) => !isDynamicPlaceholder(n) && !(n.data as DeviceNodeData).isProposal)
          .map((n) => (n.data as DeviceNodeData).device.id)
      );
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
        const nodeId =
          self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
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

      response.edges.forEach((edge) => {
        const sourceId = roleToNodeId.get(edge.source_role);
        const targetId = roleToNodeId.get(edge.target_role);
        if (!sourceId || !targetId) return;
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
        loadCanvas(ghostCanvas);
        setPreviewVersion(version);
      } catch {
        toast.error("Failed to load version");
      }
    },
    [id, nodes, edges, selectedEdgeLayer, preservedBeforePreview, loadCanvas],
  );

  const handleExitPreview = useCallback(() => {
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
          {isLiveEdit && !isReadOnly && (
            <span className="text-xs font-medium px-2 py-0.5 rounded bg-blue-100 text-blue-700">
              Editing reservation{liveReservation ? ` (${liveReservation.purpose})` : ""}
            </span>
          )}
          {isReadOnly && (
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

          {isReadOnly && !pendingProposal && (
            <AsBuiltBar deviceCount={allDeviceIds.length} onClose={handleCancelLiveEdit} />
          )}

          <ReactFlow
            onDrop={onDrop}
            onDragOver={onDragOver}
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
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

        {pendingConnection && (
          <ConnectionModal
            open={!!pendingConnection}
            sourceDeviceId={pendingConnection.sourceDeviceId}
            sourceDeviceName={pendingConnection.sourceDeviceName}
            targetDeviceId={pendingConnection.targetDeviceId}
            targetDeviceName={pendingConnection.targetDeviceName}
            defaultLayer={selectedEdgeLayer}
            onConfirm={handleConnectionConfirm}
            onCancel={handleConnectionCancel}
          />
        )}

        {/* In live-edit mode History lists the FORK's versions (ADR 0006), not the
            parent topology's, so an owner's edits never appear in the master's
            history. Read-only: P3a ships no fork version preview/diff/restore. */}
        {showHistory && isLiveEdit && (
          <ForkHistoryPanel
            versions={fork?.versions ?? []}
            onClose={() => setShowHistory(false)}
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
