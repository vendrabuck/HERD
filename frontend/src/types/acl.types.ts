export interface Grant {
  id: string;
  group_id: string;
  resource_type: "device" | "topology" | "reservation";
  resource_id: string;
  permission: "view" | "manage";
  granted_by: string | null;
  granted_at: string;
}

export interface GrantCreate {
  group_id: string;
  resource_type: "device" | "topology" | "reservation";
  resource_id: string;
  permission: "view" | "manage";
}
