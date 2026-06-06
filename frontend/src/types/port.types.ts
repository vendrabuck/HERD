export interface Port {
  id: string;
  name: string;
  device_id: string;
  template_id: string;
  template_name: string | null;
  template_icon: string | null;
  field_data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface PortCreate {
  name: string;
  template_id: string;
  field_data: Record<string, unknown>;
}

export interface PortUpdate {
  name?: string;
  field_data?: Record<string, unknown>;
}

export interface BulkPortCreate {
  name_prefix: string;
  starting_index: number;
  instances: number;
  template_id: string;
  field_data: Record<string, unknown>;
}
