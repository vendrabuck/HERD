import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
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
import { usePathfindPairs, type DevicePair } from "@/api/connections";
import { useAIStatus } from "@/api/ai";
import { useTopologyStore } from "@/stores/topologyStore";
import { EquipmentBrowser } from "@/components/equipment-browser/EquipmentBrowser";
import { FloatingPanel } from "@/components/ui/FloatingPanel";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";
import { ConnectionModal } from "@/components/topology-editor/ConnectionModal";
import { AIDialog } from "@/components/topology-editor/AIDialog";
import { AICommitDialog } from "@/components/topology-editor/AICommitDialog";
import { AIProposalBar } from "@/components/topology-editor/AIProposalBar";
import { HistoryPanel } from "@/components/topology-editor/HistoryPanel";
import { VersionDiffDialog } from "@/components/topology-editor/VersionDiffDialog";
import { RestoreConfirmDialog } from "@/components/topology-editor/RestoreConfirmDialog";
import { Modal } from "@/components/ui/Modal";
import { useCreateTemplateFromTopology } from "@/api/topologyTemplates";
import apiClient from "@/api/client";
import { DeviceNode } from "@/components/topology-editor/nodes/DeviceNode";
import { LayerEdge } from "@/components/topology-editor/edges/LayerEdge";
import type { Device } from "@/types/device.types";
import type { AIGenerateResponse } from "@/types/ai.types";
import type {
  CanvasData,
  DeviceNodeData,
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

const nodeTypes = { deviceNode: DeviceNode };
const edgeTypes = { layerEdge: LayerEdge };

const LAYER_OPTIONS: EdgeLayerType[] = ["L1", "L2", "L3"];

const LAYER_DESCRIPTIONS: Record<EdgeLayerType, string> = {
  L1: "Physical / fiber",
  L2: "Ethernet / VLAN",
  L3: "IP routing",
};

function TopologyEditorInner() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { screenToFlowPosition } = useReactFlow();
  const { data: topology, isLoading } = useTopology(id);
  const updateTopology = useUpdateTopology();

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

  // Load canvas data from backend when topology loads
  useEffect(() => {
    if (topology && !initializedRef.current) {
      initializedRef.current = true;
      if (topology.canvas_data) {
        loadCanvas(topology.canvas_data);
      } else {
        clearTopology();
      }
    }
  }, [topology, loadCanvas, clearTopology]);

  // Reset initialized ref when navigating to a different topology
  useEffect(() => {
    return () => {
      initializedRef.current = false;
    };
  }, [id]);

  const allDeviceIds = useMemo(
    () => [...new Set(nodes.map((n) => (n.data as DeviceNodeData).device.id))],
    [nodes]
  );

  const isValidConnection = useCallback(
    (connection: Connection | { source: string; target: string }): boolean => {
      const sourceNode = nodes.find((n) => n.id === connection.source);
      const targetNode = nodes.find((n) => n.id === connection.target);

      if (!sourceNode || !targetNode) return false;

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
    [addDeviceNode, screenToFlowPosition, allDeviceIds]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleConnect: OnConnect = useCallback(
    (connection) => {
      const sourceNode = nodes.find((n) => n.id === connection.source);
      const targetNode = nodes.find((n) => n.id === connection.target);
      if (!sourceNode || !targetNode) return;

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
    [nodes]
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
          .filter((n) => !(n.data as DeviceNodeData).isProposal)
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
    await updateTopology.mutateAsync({
      id,
      canvas_data: { nodes, edges, selectedEdgeLayer },
      ...(trimmed ? { description: trimmed } : {}),
    });
    setDescription("");
    toast.success("Topology saved");
  };

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
            <button
              onClick={() => setShowReserveModal(true)}
              disabled={allDeviceIds.length === 0 || hasInvalidEdges}
              title={
                hasInvalidEdges
                  ? `Cannot reserve: ${invalidEdges.length} edge${invalidEdges.length !== 1 ? "s" : ""} have no physical path or use uncabled ports`
                  : undefined
              }
              className="text-sm text-blue-600 hover:text-blue-800 px-2 py-1 rounded hover:bg-blue-50 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Reserve Topology ({allDeviceIds.length} device{allDeviceIds.length !== 1 ? "s" : ""})
            </button>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Describe this change (optional)"
              aria-label="Version description"
              disabled={!!previewVersion}
              className="text-sm border border-gray-300 rounded px-2 py-1 w-56 focus:outline-none focus:border-blue-500 disabled:bg-gray-100"
            />
            <button
              onClick={handleSave}
              disabled={updateTopology.isPending || !!previewVersion}
              className="text-sm text-green-600 hover:text-green-800 px-2 py-1 rounded hover:bg-green-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {updateTopology.isPending ? "Saving..." : "Save"}
            </button>
            <button
              onClick={() => setShowHistory((v) => !v)}
              className="text-sm text-gray-700 hover:text-gray-900 px-2 py-1 rounded hover:bg-gray-100"
            >
              History
            </button>
            <button
              onClick={() => setShowSaveAsTemplate(true)}
              disabled={!!previewVersion}
              className="text-sm text-gray-700 hover:text-gray-900 px-2 py-1 rounded hover:bg-gray-100 disabled:opacity-50"
            >
              Save as Template
            </button>
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
              disabled={!!previewVersion}
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
            fitView
            deleteKeyCode="Delete"
          >
            <Background gap={16} size={1} color="#e5e7eb" />
            <Controls />
            <MiniMap
              nodeColor={(node) => {
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

        {showHistory && id && (
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
