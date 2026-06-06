import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import apiClient from "./client";

export interface Notification {
  id: string;
  user_id: string;
  event_type: string;
  title: string;
  body: string;
  data: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface NotificationList {
  items: Notification[];
  total: number;
  unread: number;
}

export interface NotificationPreferences {
  channels: { in_app: boolean };
  events: Record<string, boolean>;
}

export interface NotificationPreferencesUpdate {
  channels?: { in_app: boolean };
  events?: Record<string, boolean>;
}

export async function fetchNotifications(params?: {
  limit?: number;
  offset?: number;
  unread_only?: boolean;
}): Promise<NotificationList> {
  const resp = await apiClient.get<NotificationList>("/notifications/notifications", {
    params,
  });
  return resp.data;
}

export async function fetchUnreadCount(): Promise<number> {
  const resp = await apiClient.get<{ count: number }>("/notifications/notifications/unread-count");
  return resp.data.count;
}

export async function markNotificationRead(id: string): Promise<Notification> {
  const resp = await apiClient.patch<Notification>(`/notifications/notifications/${id}/read`);
  return resp.data;
}

export async function markAllNotificationsRead(): Promise<number> {
  const resp = await apiClient.post<{ updated: number }>(
    "/notifications/notifications/read-all",
  );
  return resp.data.updated;
}

export async function deleteNotification(id: string): Promise<void> {
  await apiClient.delete(`/notifications/notifications/${id}`);
}

export async function fetchNotificationPreferences(): Promise<NotificationPreferences> {
  const resp = await apiClient.get<NotificationPreferences>(
    "/notifications/notifications/preferences",
  );
  return resp.data;
}

export async function updateNotificationPreferences(
  body: NotificationPreferencesUpdate,
): Promise<NotificationPreferences> {
  const resp = await apiClient.put<NotificationPreferences>(
    "/notifications/notifications/preferences",
    body,
  );
  return resp.data;
}

// --- React Query hooks ---

export function useUnreadCount(enabled: boolean = true, intervalMs: number = 30_000) {
  return useQuery({
    queryKey: ["notifications", "unread-count"],
    queryFn: fetchUnreadCount,
    enabled,
    refetchInterval: enabled ? intervalMs : false,
    staleTime: 5_000,
  });
}

export function useNotifications(params?: { limit?: number; unread_only?: boolean }) {
  return useQuery({
    queryKey: ["notifications", "list", params ?? {}],
    queryFn: () => fetchNotifications(params),
  });
}

export function useMarkNotificationRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: markNotificationRead,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

export function useMarkAllNotificationsRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: markAllNotificationsRead,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

export function useDeleteNotification() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteNotification,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

export function useNotificationPreferences() {
  return useQuery({
    queryKey: ["notifications", "preferences"],
    queryFn: fetchNotificationPreferences,
  });
}

export function useUpdateNotificationPreferences() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateNotificationPreferences,
    onSuccess: (data) => {
      queryClient.setQueryData(["notifications", "preferences"], data);
    },
  });
}
