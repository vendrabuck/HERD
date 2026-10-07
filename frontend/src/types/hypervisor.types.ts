export interface Hypervisor {
  id: string;
  name: string;
  description: string | null;
  endpoint: string;
  hypervisor_type: string;
  secret_id: string;
  enabled: boolean;
  device_group_id: string | null;
  created_at: string;
  updated_at: string;
  modified_by: string | null;
}

export interface HypervisorCreate {
  name: string;
  description?: string | null;
  endpoint: string;
  hypervisor_type: string;
  secret_id: string;
  enabled?: boolean;
  device_group_id?: string | null;
}

export interface HypervisorUpdate {
  name?: string;
  description?: string | null;
  endpoint?: string;
  hypervisor_type?: string;
  secret_id?: string;
  enabled?: boolean;
  device_group_id?: string | null;
}
