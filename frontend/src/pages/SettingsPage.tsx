import { useState } from "react";
import toast from "react-hot-toast";
import {
  useNotificationPreferences,
  useUpdateNotificationPreferences,
} from "@/api/notifications";

const EVENT_LABELS: Array<{ key: string; label: string }> = [
  { key: "reservation.created", label: "Reservation confirmed" },
  { key: "reservation.updated", label: "Reservation updated" },
  { key: "reservation.cancelled", label: "Reservation cancelled" },
  { key: "reservation.completed", label: "Reservation completed" },
];

export function SettingsPage() {
  const { data: prefs, isLoading } = useNotificationPreferences();
  const update = useUpdateNotificationPreferences();

  const [overrides, setOverrides] = useState<{
    inApp?: boolean;
    events?: Record<string, boolean>;
  }>({});

  const inApp = overrides.inApp ?? prefs?.channels.in_app ?? true;
  const events = overrides.events ?? prefs?.events ?? {};

  const handleSave = async () => {
    try {
      await update.mutateAsync({
        channels: { in_app: inApp },
        events,
      });
      setOverrides({});
      toast.success("Preferences saved");
    } catch {
      toast.error("Failed to save preferences");
    }
  };

  if (isLoading) {
    return <div className="p-6 text-sm text-gray-600">Loading preferences...</div>;
  }

  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-xl font-semibold mb-4">Settings</h1>

      <section className="bg-white rounded border border-gray-200 p-4">
        <h2 className="text-base font-semibold mb-3">Notifications</h2>

        <div className="mb-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={inApp}
              onChange={(e) =>
                setOverrides((prev) => ({ ...prev, inApp: e.target.checked }))
              }
            />
            <span>Show in-app notifications</span>
          </label>
          <p className="text-xs text-gray-500 mt-1">
            Disable to stop the bell from showing new items. Existing notifications remain visible
            until you clear them.
          </p>
        </div>

        <div className="mb-4">
          <div className="text-sm font-medium mb-2">Notify me about</div>
          <div className="space-y-1">
            {EVENT_LABELS.map(({ key, label }) => (
              <label key={key} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={events[key] ?? true}
                  onChange={(e) =>
                    setOverrides((prev) => ({
                      ...prev,
                      events: { ...(prev.events ?? events), [key]: e.target.checked },
                    }))
                  }
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </div>

        <button
          type="button"
          onClick={handleSave}
          disabled={update.isPending}
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {update.isPending ? "Saving..." : "Save"}
        </button>
      </section>
    </div>
  );
}
