export interface LdapSyncStatus {
  auth_method: "local" | "ldap";
  group_sync_enabled: boolean;
  sync_interval_seconds: number;
}

export interface LdapGroupMapping {
  id: string;
  group_dn: string;
  directory_name: string;
  herd_group_id: string;
  created_by: string | null;
  created_at: string;
}

export interface LdapGroupMappingCreate {
  group_dn: string;
  herd_group_id: string;
}

export interface LdapGroupMappingCreateResult extends LdapGroupMapping {
  warning: string | null;
}

export type LdapSyncRunStatus = "success" | "partial" | "aborted" | "failed" | "running";

export interface LdapSyncRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  trigger: "interval" | "manual";
  status: LdapSyncRunStatus;
  users_provisioned: number;
  members_added: number;
  members_removed: number;
  members_skipped: number;
  users_deactivated: number;
  users_reactivated: number;
  detail: Record<string, unknown>;
  error: string | null;
}

export interface LdapSyncRunStart {
  run_id: string;
}
