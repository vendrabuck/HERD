import { useDeviceGroupsForDevice } from "@/api/deviceGroups";
import type { Device } from "@/types/device.types";
import { DeviceHealthBadge } from "./DeviceHealthBadge";

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

export function DeviceInfoPanel({ device }: { device: Device }) {
  const { data: memberships, isLoading } = useDeviceGroupsForDevice(device.id);

  const vendor = device.template_vendor;
  const model = device.template_model;
  const showIdentityChip =
    !!vendor && !!model && vendor !== "unknown" && model !== "unknown";

  return (
    <div className="space-y-3 text-sm min-w-[220px]">
      {showIdentityChip && (
        <div>
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Hardware</h4>
          <span className="inline-block px-2 py-0.5 text-xs font-medium text-gray-700 bg-gray-100 rounded">
            {vendor}, {model}
          </span>
        </div>
      )}
      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Health</h4>
        <div className="flex items-center gap-2">
          <DeviceHealthBadge deviceId={device.id} />
          {device.resolved_poll_interval_seconds !== null && (
            <span className="text-xs text-gray-500">
              polled every {device.resolved_poll_interval_seconds}s
            </span>
          )}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Dates</h4>
        <dl className="space-y-0.5">
          <div className="flex gap-1">
            <dt className="text-gray-500">Created:</dt>
            <dd className="text-gray-900">{formatDate(device.created_at)}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-gray-500">Modified:</dt>
            <dd className="text-gray-900">{formatDate(device.updated_at)}</dd>
          </div>
        </dl>
      </div>
      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Audit</h4>
        <dl className="space-y-0.5">
          <div className="flex gap-1">
            <dt className="text-gray-500">Created by:</dt>
            <dd className="text-gray-900">{device.created_by_name || "Unknown"}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-gray-500">Modified by:</dt>
            <dd className="text-gray-900">{device.modified_by_name || "Unknown"}</dd>
          </div>
        </dl>
      </div>
      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Device Groups</h4>
        {isLoading ? (
          <p className="text-gray-400">Loading...</p>
        ) : !memberships || memberships.length === 0 ? (
          <p className="text-gray-400">None</p>
        ) : (
          <ul className="space-y-1.5">
            {memberships.map((m) => (
              <li key={m.id}>
                <span className="font-medium text-gray-900">{m.name}</span>
                {m.user_groups.length > 0 && (
                  <ul className="ml-3 mt-0.5 space-y-0.5">
                    {m.user_groups.map((ug) => (
                      <li key={ug.user_group_id} className="text-gray-600 text-xs">
                        {ug.user_group_name || ug.user_group_id.slice(0, 8) + "..."}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
