import type { Node, Edge } from "@xyflow/react";
import type { Device, TopologyType } from "./device.types";

export type EdgeLayerType = "L1" | "L2" | "L3";

export interface DeviceNodeData extends Record<string, unknown> {
  device: Device;
  label: string;
  topologyType: TopologyType;
}

export interface LayerEdgeData extends Record<string, unknown> {
  layer: EdgeLayerType;
}

export type DeviceNode = Node<DeviceNodeData, "deviceNode">;
export type LayerEdge = Edge<LayerEdgeData>;
