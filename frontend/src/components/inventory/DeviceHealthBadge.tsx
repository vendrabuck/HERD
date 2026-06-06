import { useDeviceHealth } from "@/api/health";
import type { HealthStatus } from "@/api/health";
import { StatusBadge } from "@/components/ui/StatusBadge";

const STATUS_LABELS: Record<HealthStatus, string> = {
  HEALTHY: "Healthy",
  DEGRADED: "Degraded",
  UNREACHABLE: "Unreachable",
  UNKNOWN: "Unknown",
};

function formatLastPolled(iso: string | null): string {
  if (!iso) return "Never polled";
  return `Last polled: ${new Date(iso).toLocaleString()}`;
}

export function DeviceHealthBadge({ deviceId }: { deviceId: string }) {
  const { data, isLoading } = useDeviceHealth(deviceId);
  if (isLoading || !data) {
    return (
      <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-gray-100 text-gray-400">
        ...
      </span>
    );
  }
  const status = data.last_status;
  return (
    <StatusBadge
      status={status}
      label={STATUS_LABELS[status]}
      title={formatLastPolled(data.last_polled_at)}
    />
  );
}
