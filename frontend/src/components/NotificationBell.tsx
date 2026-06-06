import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import {
  useDeleteNotification,
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
  useUnreadCount,
  type Notification,
} from "@/api/notifications";

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const now = Date.now();
  const secs = Math.max(1, Math.floor((now - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function NotificationBell() {
  const navigate = useNavigate();
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const { data: unreadCount = 0 } = useUnreadCount(isAuthenticated);
  const { data: list, refetch: refetchList } = useNotifications({ limit: 20 });
  const markRead = useMarkNotificationRead();
  const markAll = useMarkAllNotificationsRead();
  const del = useDeleteNotification();

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  useEffect(() => {
    if (open) refetchList();
  }, [open, refetchList]);

  const handleItemClick = (n: Notification) => {
    if (!n.read_at) {
      markRead.mutate(n.id);
    }
  };

  const handleDelete = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    del.mutate(id);
  };

  if (!isAuthenticated) return null;

  return (
    <div className="relative" ref={panelRef}>
      <button
        type="button"
        aria-label="Notifications"
        onClick={() => setOpen((v) => !v)}
        className="relative text-sm text-gray-400 hover:text-white px-2 py-1 rounded hover:bg-gray-700 transition-colors"
      >
        <span aria-hidden className="inline-block w-5 h-5 align-middle">
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5">
            <path d="M10 2a6 6 0 00-6 6v3.586l-.707.707A1 1 0 004 14h12a1 1 0 00.707-1.707L16 11.586V8a6 6 0 00-6-6zm0 16a2 2 0 01-2-2h4a2 2 0 01-2 2z" />
          </svg>
        </span>
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 text-[10px] font-bold bg-red-500 text-white rounded-full px-1.5 min-w-[18px] text-center leading-4">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-96 bg-white rounded shadow-lg z-50 text-gray-900 border border-gray-200">
          <div className="flex items-center justify-between px-4 py-2 border-b border-gray-200">
            <span className="font-semibold text-sm">Notifications</span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={!unreadCount || markAll.isPending}
                onClick={() => markAll.mutate()}
                className="text-xs text-gray-600 hover:text-gray-900 disabled:opacity-40"
              >
                Mark all read
              </button>
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  navigate("/settings");
                }}
                className="text-xs text-gray-600 hover:text-gray-900"
              >
                Settings
              </button>
            </div>
          </div>
          <div className="max-h-96 overflow-y-auto">
            {!list || list.items.length === 0 ? (
              <div className="p-6 text-center text-sm text-gray-500">
                No notifications yet.
              </div>
            ) : (
              list.items.map((n) => (
                <button
                  type="button"
                  key={n.id}
                  onClick={() => handleItemClick(n)}
                  className={`w-full text-left px-4 py-3 border-b border-gray-100 hover:bg-gray-50 transition-colors ${
                    n.read_at ? "bg-white" : "bg-blue-50"
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-gray-900">{n.title}</div>
                      <div className="text-xs text-gray-600 mt-0.5">{n.body}</div>
                      <div className="text-[11px] text-gray-400 mt-1">
                        {formatRelative(n.created_at)}
                      </div>
                    </div>
                    <button
                      type="button"
                      aria-label="Delete notification"
                      onClick={(e) => handleDelete(e, n.id)}
                      className="text-gray-400 hover:text-red-500 text-sm"
                    >
                      x
                    </button>
                  </div>
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
