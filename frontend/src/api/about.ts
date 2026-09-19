import { useQueries } from "@tanstack/react-query";
import apiClient from "./client";

export interface ServiceVersion {
  service: string;
  version: string;
  build: string;
  build_date: string | null;
}

export interface ServiceDescriptor {
  /** Stable key; also the "service" field the backend's own /version reports. */
  name: string;
  /** Display label for the About page table. */
  label: string;
  /** Path relative to apiClient's "/api" base (apiClient prepends "/api"). */
  path: string;
}

// The twelve backend services and the gateway path each answers GET /version
// on (issue #846). Most map name to path 1:1; two are irregular, per the
// fixed interface contract: integration answers under its versioned facade
// prefix (/api/v1/version, not /api/integration/version), which is why its
// path below is "/v1/version" and not "/integration/version".
export const SERVICES: ServiceDescriptor[] = [
  { name: "acl", label: "ACL", path: "/acl/version" },
  { name: "ai-orchestrator", label: "AI Orchestrator", path: "/ai/version" },
  { name: "auth", label: "Auth", path: "/auth/version" },
  { name: "cabling", label: "Cabling", path: "/cabling/version" },
  { name: "config", label: "Config", path: "/config/version" },
  { name: "execution", label: "Execution", path: "/execution/version" },
  { name: "integration", label: "Integration", path: "/v1/version" },
  { name: "inventory", label: "Inventory", path: "/inventory/version" },
  { name: "notifications", label: "Notifications", path: "/notifications/version" },
  { name: "reservations", label: "Reservations", path: "/reservations/version" },
  { name: "secrets", label: "Secrets", path: "/secrets/version" },
  { name: "user-profile", label: "User Profile", path: "/user-profile/version" },
];

export async function fetchServiceVersion(service: ServiceDescriptor): Promise<ServiceVersion> {
  const resp = await apiClient.get<ServiceVersion>(service.path);
  return resp.data;
}

/**
 * Fetches every service's /version INDEPENDENTLY (useQueries, one query per
 * service, not one combined call), so a single unreachable or slow service
 * cannot blank the rest of the About page's table. Each returned entry
 * carries its own ServiceDescriptor alongside the query result so the
 * caller never has to re-zip SERVICES back onto the results array.
 */
export function useServiceVersions() {
  const results = useQueries({
    queries: SERVICES.map((service) => ({
      queryKey: ["about", "version", service.name],
      queryFn: () => fetchServiceVersion(service),
      // No retry storm: an unreachable service should settle into
      // "unreachable" promptly (this page's whole purpose is to surface
      // that fact), not spend seconds retrying with backoff first.
      retry: false,
      staleTime: 60_000,
    })),
  });

  return SERVICES.map((service, i) => ({ service, ...results[i] }));
}
