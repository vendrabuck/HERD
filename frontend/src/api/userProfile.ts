import apiClient from "./client";

export interface Preferences {
  user_id: string;
  saved_filters: Record<string, unknown>;
  page_sizes: Record<string, number>;
  extras: Record<string, unknown>;
  updated_at: string;
}

export interface PreferencesPatch {
  saved_filters?: Record<string, unknown>;
  page_sizes?: Record<string, number>;
  extras?: Record<string, unknown>;
}

export async function getPreferences(): Promise<Preferences> {
  const resp = await apiClient.get<Preferences>("/user-profile/preferences");
  return resp.data;
}

export async function patchPreferences(patch: PreferencesPatch): Promise<Preferences> {
  const resp = await apiClient.patch<Preferences>("/user-profile/preferences", patch);
  return resp.data;
}

export async function resetPreferences(): Promise<Preferences> {
  const resp = await apiClient.delete<Preferences>("/user-profile/preferences");
  return resp.data;
}
