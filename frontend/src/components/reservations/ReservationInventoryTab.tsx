import { useState, useMemo } from "react";
import { Link } from "react-router-dom";
import { useDevice, useAllDeviceNames } from "@/api/inventory";
import { usePorts } from "@/api/ports";
import { useDeviceConnections } from "@/api/connections";

interface Props {
  deviceIds: string[];
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className={`w-4 h-4 text-gray-400 transition-transform ${expanded ? "rotate-90" : ""}`}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
    </svg>
  );
}

function DevicePortsDetail({
  deviceId,
  deviceNameMap,
}: {
  deviceId: string;
  deviceNameMap: Map<string, string> | undefined;
}) {
  const { data: ports, isLoading: portsLoading } = usePorts(deviceId);
  const { data: connections } = useDeviceConnections(deviceId);

  const connectionMap = useMemo(() => {
    const map = new Map<string, { deviceId: string; deviceName: string; portName: string }>();
    if (!connections) return map;
    for (const conn of connections) {
      const isA = conn.device_a_id === deviceId;
      const thisPort = isA ? conn.port_a : conn.port_b;
      const otherDeviceId = isA ? conn.device_b_id : conn.device_a_id;
      const otherPort = isA ? conn.port_b : conn.port_a;
      const otherName = deviceNameMap?.get(otherDeviceId) ?? otherDeviceId.slice(0, 8) + "...";
      map.set(thisPort, { deviceId: otherDeviceId, deviceName: otherName, portName: otherPort });
    }
    return map;
  }, [connections, deviceId, deviceNameMap]);

  if (portsLoading) {
    return <p className="text-xs text-gray-400 px-4 py-2">Loading ports...</p>;
  }

  if (!ports || ports.length === 0) {
    return <p className="text-xs text-gray-400 px-4 py-2">No ports</p>;
  }

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="bg-gray-50">
          <th className="px-4 py-1.5 text-left font-medium text-gray-500">Port</th>
          <th className="px-4 py-1.5 text-left font-medium text-gray-500">Connected To</th>
        </tr>
      </thead>
      <tbody>
        {ports.map((port) => {
          const conn = connectionMap.get(port.name);
          return (
            <tr key={port.id} className="border-t border-gray-100">
              <td className="px-4 py-1.5 text-gray-700">{port.name}</td>
              <td className="px-4 py-1.5">
                {conn ? (
                  <Link
                    to={`/inventory/${conn.deviceId}`}
                    className="text-blue-600 hover:underline"
                  >
                    {conn.deviceName}, {conn.portName}
                  </Link>
                ) : (
                  <span className="text-gray-300">-</span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function DeviceRow({
  deviceId,
  deviceNameMap,
}: {
  deviceId: string;
  deviceNameMap: Map<string, string> | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const { data: device } = useDevice(deviceId);

  const name = device?.name ?? deviceNameMap?.get(deviceId) ?? deviceId.slice(0, 8) + "...";
  const templateName = device?.template_name ?? "-";
  const status = device?.status ?? "-";

  const statusColor: Record<string, string> = {
    AVAILABLE: "bg-green-100 text-green-800",
    RESERVED: "bg-yellow-100 text-yellow-800",
    OFFLINE: "bg-gray-100 text-gray-600",
    MAINTENANCE: "bg-red-100 text-red-700",
  };

  return (
    <div className="border-b border-gray-100 last:border-b-0">
      <div
        className="flex items-center gap-3 px-4 py-2.5 cursor-pointer hover:bg-gray-50"
        onClick={() => setExpanded(!expanded)}
      >
        <ChevronIcon expanded={expanded} />
        <span className="text-sm font-medium text-gray-800 flex-1">{name}</span>
        <span className="text-xs text-gray-400">{templateName}</span>
        <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${statusColor[status] ?? "bg-gray-100 text-gray-600"}`}>
          {status}
        </span>
      </div>
      {expanded && (
        <div className="pl-7 pb-2">
          <DevicePortsDetail deviceId={deviceId} deviceNameMap={deviceNameMap} />
        </div>
      )}
    </div>
  );
}

export function ReservationInventoryTab({ deviceIds }: Props) {
  const { data: deviceNameMap } = useAllDeviceNames();

  if (deviceIds.length === 0) {
    return <p className="text-sm text-gray-400 text-center py-8">No devices in this reservation.</p>;
  }

  return (
    <div className="space-y-4">
      {/* Device list */}
      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">
          Devices ({deviceIds.length})
        </h4>
        <div className="border border-gray-200 rounded overflow-hidden">
          {deviceIds.map((id) => (
            <DeviceRow key={id} deviceId={id} deviceNameMap={deviceNameMap} />
          ))}
        </div>
      </div>
    </div>
  );
}
