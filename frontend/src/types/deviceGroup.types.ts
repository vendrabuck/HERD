export interface DeviceGroupDevice {
  device_id: string;
  device_name: string | null;
  template_name: string | null;
  added_at: string;
}

export interface DeviceGroupPermission {
  user_group_id: string;
  user_group_name: string | null;
  assigned_by: string | null;
  assigned_at: string;
}

export interface DeviceGroup {
  id: string;
  name: string;
  description: string | null;
  created_by: string | null;
  created_at: string;
  device_count: number;
  user_group_count: number;
}

export interface DeviceGroupDetail extends DeviceGroup {
  devices: DeviceGroupDevice[];
  user_groups: DeviceGroupPermission[];
}

export interface DeviceGroupMembership {
  id: string;
  name: string;
  description: string | null;
  user_groups: DeviceGroupPermission[];
}

export interface DeviceGroupCreate {
  name: string;
  description?: string | null;
}

export interface DeviceGroupUpdate {
  name?: string;
  description?: string | null;
}

export interface BulkResult {
  added: number;
  skipped: number;
}

export interface BulkRemoveResult {
  removed: number;
  not_found: number;
}
