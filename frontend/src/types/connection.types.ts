export interface Connection {
  id: string;
  device_a_id: string;
  port_a: string;
  device_b_id: string;
  port_b: string;
  connection_type: string;
  notes: string | null;
  created_by: string;
  created_at: string;
  modified_by: string | null;
  updated_at: string | null;
}

export interface PathHop {
  device_id: string;
  port_in: string | null;
  port_out: string | null;
}

export interface PathfindResponse {
  reachable: boolean;
  hop_count: number;
  paths: PathHop[][];
}

export interface ConnectionCreate {
  device_a_id: string;
  port_a: string;
  device_b_id: string;
  port_b: string;
  connection_type?: string;
  notes?: string;
}
