import { useState } from "react";
import toast from "react-hot-toast";
import {
  useNotificationPreferences,
  useUpdateNotificationPreferences,
  type NotificationChannels,
} from "@/api/notifications";

const EVENT_LABELS: Array<{ key: string; label: string }> = [
  { key: "reservation.created", label: "Reservation confirmed" },
  { key: "reservation.updated", label: "Reservation updated" },
  { key: "reservation.cancelled", label: "Reservation cancelled" },
  { key: "reservation.completed", label: "Reservation completed" },
  { key: "reservation.expiring_soon", label: "Reservation expiring soon" },
];

const OUTBOUND_CHANNELS: Array<{
  key: "email" | "chat" | "webhook";
  label: string;
  hint: string;
}> = [
  { key: "email", label: "Email", hint: "Send notifications to your account email." },
  { key: "chat", label: "Chat", hint: "Post notifications to the configured chat channel." },
  { key: "webhook", label: "Webhook", hint: "POST notifications to the configured webhook." },
];

const DEFAULT_CHANNELS: NotificationChannels = {
  in_app: true,
  email: false,
  chat: false,
  webhook: false,
};

export function SettingsPage() {
  const { data: prefs, isLoading } = useNotificationPreferences();
  const update = useUpdateNotificationPreferences();

  const [overrides, setOverrides] = useState<{
    channels?: Partial<NotificationChannels>;
    events?: Record<string, boolean>;
  }>({});

  const channels: NotificationChannels = {
    ...DEFAULT_CHANNELS,
    ...(prefs?.channels ?? {}),
    ...(overrides.channels ?? {}),
  };
  const events = overrides.events ?? prefs?.events ?? {};

  const setChannel = (key: keyof NotificationChannels, value: boolean) =>
    setOverrides((prev) => ({
      ...prev,
      channels: { ...(prev.channels ?? {}), [key]: value },
    }));

  const handleSave = async () => {
    try {
      await update.mutateAsync({
        channels,
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
              checked={channels.in_app}
              onChange={(e) => setChannel("in_app", e.target.checked)}
            />
            <span>Show in-app notifications</span>
          </label>
          <p className="text-xs text-gray-500 mt-1">
            Disable to stop the bell from showing new items. Existing notifications remain visible
            until you clear them.
          </p>
        </div>

        <div className="mb-4">
          <div className="text-sm font-medium mb-2">Outbound channels</div>
          <p className="text-xs text-gray-500 mb-2">
            Off by default. Enable to also receive notifications outside the app. Transport is
            configured by your administrator; a channel does nothing until both you opt in and the
            transport is set up.
          </p>
          <div className="space-y-2">
            {OUTBOUND_CHANNELS.map(({ key, label, hint }) => (
              <div key={key}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={channels[key]}
                    onChange={(e) => setChannel(key, e.target.checked)}
                  />
                  <span>{label}</span>
                </label>
                <p className="text-xs text-gray-500 ml-6">{hint}</p>
              </div>
            ))}
          </div>
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
