import axios from "axios";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useConfigStore } from "@/stores/configStore";

const configClient = axios.create({
  baseURL: "/api/config",
  headers: { "Content-Type": "application/json" },
});

configClient.interceptors.request.use((config) => {
  const token = useConfigStore.getState().configToken;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// -- Types --

export interface ConfigField {
  key: string;
  label: string;
  type: "string" | "password" | "number" | "boolean" | "dropdown";
  required: boolean;
  group: string;
  secret: boolean;
  description: string;
  default?: string | number | boolean;
  options?: string[];
}

export interface ConfigStatus {
  configured: boolean;
  password_changed: boolean;
}

// -- Queries --

export function useConfigStatus() {
  return useQuery<ConfigStatus>({
    queryKey: ["configStatus"],
    queryFn: async () => {
      const { data } = await configClient.get("/status");
      return data;
    },
    staleTime: 10_000,
    retry: 1,
  });
}

export function useConfigSchema() {
  return useQuery<ConfigField[]>({
    queryKey: ["configSchema"],
    queryFn: async () => {
      const { data } = await configClient.get("/schema");
      return data.fields;
    },
    staleTime: 60_000,
  });
}

export function useConfigSettings() {
  const token = useConfigStore((s) => s.configToken);
  return useQuery<Record<string, string | number | boolean>>({
    queryKey: ["configSettings"],
    queryFn: async () => {
      const { data } = await configClient.get("/settings");
      return data.values;
    },
    enabled: !!token,
    staleTime: 5_000,
  });
}

// -- Mutations --

export function useConfigLogin() {
  const setConfigToken = useConfigStore((s) => s.setConfigToken);
  return useMutation({
    mutationFn: async (password: string) => {
      const { data } = await configClient.post("/login", { password });
      return data as { token: string; password_changed: boolean };
    },
    onSuccess: (data) => {
      setConfigToken(data.token);
    },
  });
}

export function useChangeConfigPassword() {
  return useMutation({
    mutationFn: async (newPassword: string) => {
      const { data } = await configClient.post("/change-password", {
        new_password: newPassword,
      });
      return data;
    },
  });
}

export function useSaveConfigSettings() {
  return useMutation({
    mutationFn: async (values: Record<string, string | number | boolean>) => {
      const { data } = await configClient.put("/settings", { values });
      return data;
    },
  });
}

export function useApplyConfig() {
  return useMutation({
    mutationFn: async () => {
      const { data } = await configClient.post("/apply");
      return data as { restarted: string[]; errors: string[] };
    },
  });
}
