import type { Node, Edge } from "@xyflow/react";
import type { Device, TopologyType } from "./device.types";

export type EdgeLayerType = "L1" | "L2" | "L3";

export interface DeviceNodeData extends Record<string, unknown> {
  device: Device;
  label: string;
  topologyType: TopologyType;
  isProposal?: boolean;
}

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
}

export type DeviceNode = Node<DeviceNodeData, "deviceNode">;
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
  nodes: Node<DeviceNodeData>[];
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
