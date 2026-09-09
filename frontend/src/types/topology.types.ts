import type { Node, Edge } from "@xyflow/react";
import type { Device, TopologyType } from "./device.types";

export type EdgeLayerType = "L1" | "L2" | "L3";

// One routing-intent entry on a Layer 3 Switch device node (ADR 0014 Decision
// 1, issue #34). Mirrors the cabling `RouteSpec` canvas shape exactly: no
// extra keys, since the backend parser refuses anything outside
// {destination, next_hop, interface, virtual_router}. A blank next hop or
// virtual router is stored as null, never an empty string (matches the
// server's own `_require_string` normalization).
export interface L3RouteIntent {
  destination: string;
  next_hop: string | null;
  interface: string;
  virtual_router: string | null;
}

export interface DeviceNodeData extends Record<string, unknown> {
  device: Device;
  label: string;
  topologyType: TopologyType;
  isProposal?: boolean;
  // Layer 3 routing intent (ADR 0014, issue #34 phase 2): present only on a
  // node whose device is a "Layer 3 Switch" and only once at least one route
  // has been added. Absent, not an empty `{routes: []}`, once the last route
  // is removed (matches the backend's R10 "empty intent is no intent" rule).
  // Persists through `persistableCanvas` untouched: it is ordinary node data,
  // and `stripTransientEdgeFields` only ever touches edges.
  l3?: { routes: L3RouteIntent[] };
  // Render-only, never persisted (issue #34 E4/E5): true when the last
  // validation run reported an `invalid_routes` entry for this node. Set by
  // TopologyEditorPage on a derived render view of `nodes` it builds just
  // before handing them to ReactFlow, never on the topology store's own
  // `nodes` state, so it can never leak into `persistableCanvas` (the same
  // transient-field precedent as LayerEdgeData's `diffStatus`).
  l3ValidationInvalid?: boolean;
}

// A canvas-local planning artifact for a dynamic (hypervisor-backed) template:
// one node per template with an editable instance count. It carries no inventory
// device id and is never persisted into a topology's canvas_data or device set;
// at reserve time count expands into repeated {template_id} dynamic_requests.
export interface DynamicPlaceholderNodeData extends Record<string, unknown> {
  templateId: string;
  templateName: string;
  templateIcon: string | null;
  count: number;
}

// The closed v1 element vocabulary (ADR 0012 Decision, "Canvas shape"). The
// three motivating examples plus patch_trunk, the one type with a plausible
// physical realization.
export type NetworkElementType = "vlan_segment" | "subnet" | "external_cloud" | "patch_trunk";

// A network element's own identity, distinct from the React Flow node id: it
// is minted client-side at drop time (no server registry, ADR 0012 Decision
// 1) and stays stable across a copy-paste or re-layout. `attrs` is free-form
// and DESCRIPTIVE only in v1 (ADR 0012 "Canvas shape"): nothing reads it yet.
export interface NetworkElementData {
  id: string;
  element_type: NetworkElementType;
  label: string;
  attrs: Record<string, unknown>;
}

// Unlike DynamicPlaceholderNodeData, this node type is the OPPOSITE of a
// placeholder: it persists into canvas_data (ADR 0012 "Canvas shape", the
// element predicate is deliberately absent from the persistableCanvas strip
// filter). It carries no `device` field, same precedent as the placeholder.
export interface NetworkElementNodeData extends Record<string, unknown> {
  element: NetworkElementData;
  isProposal?: boolean;
}

export type CanvasNodeData = DeviceNodeData | DynamicPlaceholderNodeData | NetworkElementNodeData;

export interface LayerEdgeData extends Record<string, unknown> {
  layer: EdgeLayerType;
  source_port_id?: string;
  source_port_name?: string;
  target_port_id?: string;
  target_port_name?: string;
  pathValid?: boolean | null;
  pathHopCount?: number;
  portsCabled?: boolean | null;
  isProposal?: boolean;
  // Set only on the read-only overlay canvas a fork version diff renders
  // (issue #622, lib/forkDiff.ts buildForkDiffOverlayCanvas): "added" for a
  // wire present in the compare side but not the base, "removed" for one
  // synthesized back in from the base side. Never persisted; the overlay
  // canvas is never autosaved (the editor locks while it is loaded).
  diffStatus?: "added" | "removed";
}

export type DeviceNode = Node<DeviceNodeData, "deviceNode">;
export type DynamicPlaceholderNode = Node<DynamicPlaceholderNodeData, "dynamicPlaceholderNode">;
export type NetworkElementNode = Node<NetworkElementNodeData, "networkElementNode">;
export type LayerEdge = Edge<LayerEdgeData>;

export interface Topology {
  id: string;
  name: string;
  created_by: string;
  owner_name: string;
  created_at: string;
  updated_at: string;
  canvas_data?: CanvasData | null;
}

export interface CanvasData {
  nodes: Node<CanvasNodeData>[];
  edges: Edge<LayerEdgeData>[];
  selectedEdgeLayer?: EdgeLayerType;
}

export interface TopologyCreate {
  name: string;
}

export interface TopologyUpdate {
  name?: string;
  canvas_data?: CanvasData | null;
  description?: string;
}

export interface TopologyVersion {
  id: string;
  topology_id: string;
  version_number: number;
  name: string;
  description: string | null;
  created_by: string;
  author_name: string;
  created_at: string;
  restored_from_id: string | null;
}

export interface TopologyVersionDetail extends TopologyVersion {
  canvas_data: CanvasData | null;
}

export interface ModifiedItem {
  id: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
}

export interface TopologyDiff {
  version_a: string;
  version_b: string;
  nodes_added: Array<Record<string, unknown>>;
  nodes_removed: Array<Record<string, unknown>>;
  nodes_modified: ModifiedItem[];
  edges_added: Array<Record<string, unknown>>;
  edges_removed: Array<Record<string, unknown>>;
  edges_modified: ModifiedItem[];
}

export interface RestoreRequest {
  description?: string;
  restore_name?: boolean;
}

// One Layer 3 routing-intent problem `POST /topologies/{id}/validate` found
// (ADR 0014 phase 1, issue #34). Mirrors cabling's `InvalidRoute` schema
// exactly. `index` is null for a switch-level refusal (l3_malformed,
// l3_not_a_router, l3_switch_unconfigured, l3_switch_unattached); `detail`
// carries the parser's message only for `l3_malformed`. The full reason
// vocabulary is documented in docs/TOPOLOGY_EDITOR.md and
// services/cabling/app/services/topology_validation.py.
export interface InvalidRoute {
  node_id: string;
  device_id: string | null;
  index: number | null;
  reason: string;
  detail: string | null;
}

// POST /topologies/{id}/validate response (additive `invalid_routes` on top
// of the pre-existing edge validation, ADR 0014 phase 1). The editor only
// ever reads `invalid_routes`; `invalid_edges`/`device_ids` are already
// covered by the client-side pathfind checks this call does not replace.
export interface TopologyValidationResponse {
  valid: boolean;
  invalid_edges: Array<Record<string, unknown>>;
  device_ids: string[];
  invalid_routes: InvalidRoute[];
}
