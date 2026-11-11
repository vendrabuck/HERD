import { useEffect, useId, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate, useLocation } from "react-router-dom";
import { useCurrentUser, useLogout } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { HelpCircle } from "lucide-react";
import { NotificationBell } from "@/components/NotificationBell";
import { APP_VERSION } from "@/lib/appVersion";
import { MANUAL_URL } from "@/lib/links";
import { displayName } from "@/lib/profileEdit";
import { visibleAdminNav } from "@/lib/adminNav";
import { useRouteTitle } from "@/hooks/useRouteTitle";

const NAV_ITEMS = [
  { to: "/inventory", label: "Inventory" },
  { to: "/templates", label: "Templates" },
  { to: "/topology", label: "Topology" },
  { to: "/reservations", label: "Reservations" },
  { to: "/reporting", label: "Reporting" },
];

export function AppLayout() {
  const { data: user } = useCurrentUser();
  const logout = useLogout();
  const navigate = useNavigate();
  const location = useLocation();
  // How the Administration disclosure was opened: "hover" closes again on
  // mouse leave, "toggle" (click, Enter, Space) stays open until toggled,
  // Escape, an outside click, focus leaving it, or a route change.
  const [adminOpenedBy, setAdminOpenedBy] = useState<"hover" | "toggle" | null>(null);
  const adminOpen = adminOpenedBy !== null;
  const adminMenuId = useId();
  const adminButtonRef = useRef<HTMLButtonElement>(null);
  const adminWrapperRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const [menuPathname, setMenuPathname] = useState(location.pathname);
  if (menuPathname !== location.pathname) {
    setMenuPathname(location.pathname);
    setAdminOpenedBy(null);
  }
  useRouteTitle(location.pathname);

  useEffect(() => {
    if (!adminOpen) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!adminWrapperRef.current?.contains(e.target as Node)) setAdminOpenedBy(null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [adminOpen]);

  useEffect(() => {
    useAuthStore.getState().setUser(user ?? null);
    if (user) usePreferencesStore.getState().load();
  }, [user]);

  const handleLogout = async () => {
    await logout.mutateAsync();
    usePreferencesStore.getState().clear();
    navigate("/login");
  };

  const adminLinks = visibleAdminNav(user?.role);
  const adminActive = location.pathname.startsWith("/admin");

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-gray-100">
      <a
        href="#main-content"
        onClick={(e) => {
          e.preventDefault();
          mainRef.current?.focus();
        }}
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:px-3 focus:py-2 focus:rounded focus:bg-white focus:text-blue-600 focus:text-sm focus:ring-2 focus:ring-blue-500"
      >
        Skip to main content
      </a>
      <header className="flex items-center justify-between px-4 py-2 bg-gray-900 text-white shrink-0 border-b border-gray-800">
        <div className="flex items-center gap-4">
          <span className="flex items-baseline gap-1.5">
            <span className="font-bold text-lg tracking-tight">HERD</span>
            <span data-testid="app-version-header" className="text-xs text-gray-400 font-normal">
              v{APP_VERSION}
            </span>
          </span>
          <nav aria-label="Main" className="flex gap-1">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to !== "/reservations"}
                className={({ isActive }) =>
                  `text-sm px-3 py-1 rounded transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                    isActive || (item.to === "/reservations" && location.pathname.startsWith("/reservations"))
                      ? "bg-gray-700 text-white"
                      : "text-gray-300 hover:text-white hover:bg-gray-800"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
            {adminLinks.length > 0 && (
              // A disclosure, not an ARIA menu (issue #1317): the button
              // toggles on click, Enter, or Space and exposes aria-expanded;
              // the entries are ordinary links reached with Tab. Hover still
              // opens it for mouse users; a click while hover-opened pins it.
              <div
                ref={adminWrapperRef}
                className="relative"
                onMouseEnter={() => setAdminOpenedBy((by) => by ?? "hover")}
                onMouseLeave={() => setAdminOpenedBy((by) => (by === "hover" ? null : by))}
                onKeyDown={(e) => {
                  if (e.key === "Escape" && adminOpen) {
                    e.stopPropagation();
                    setAdminOpenedBy(null);
                    adminButtonRef.current?.focus();
                  }
                }}
                onBlur={(e) => {
                  const next = e.relatedTarget as Node | null;
                  if (next && !e.currentTarget.contains(next)) setAdminOpenedBy(null);
                }}
              >
                <button
                  ref={adminButtonRef}
                  type="button"
                  aria-expanded={adminOpen}
                  aria-controls={adminMenuId}
                  onClick={() =>
                    setAdminOpenedBy((by) => (by === "hover" ? "toggle" : by ? null : "toggle"))
                  }
                  className={`text-sm px-3 py-1 rounded transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                    adminActive
                      ? "bg-gray-700 text-white"
                      : "text-gray-300 hover:text-white hover:bg-gray-800"
                  }`}
                >
                  Administration
                </button>
                <ul
                  id={adminMenuId}
                  hidden={!adminOpen}
                  className="absolute left-0 top-full bg-gray-800 rounded shadow-lg py-1 min-w-40 z-50"
                >
                  {adminLinks.map((item) => (
                    <li key={item.to}>
                      <NavLink
                        to={item.to}
                        onClick={() => setAdminOpenedBy(null)}
                        className={({ isActive }) =>
                          `block px-4 py-2 text-sm transition-colors focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-500 ${
                            isActive
                              ? "bg-gray-700 text-white"
                              : "text-gray-300 hover:text-white hover:bg-gray-700"
                          }`
                        }
                      >
                        {item.label}
                      </NavLink>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </nav>
        </div>
        <div className="flex items-center gap-3">
          <a
            href={MANUAL_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Help"
            className="text-gray-300 hover:text-white p-1 rounded hover:bg-gray-700 transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <HelpCircle size={18} />
          </a>
          <NotificationBell />
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              `text-sm px-2 py-1 rounded transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                isActive
                  ? "bg-gray-700 text-white"
                  : "text-gray-300 hover:text-white hover:bg-gray-700"
              }`
            }
          >
            Settings
          </NavLink>
          {user && (
            <span className="text-sm text-gray-300" title={user.username}>
              {displayName(user)}
            </span>
          )}
          <button
            onClick={handleLogout}
            disabled={logout.isPending}
            className="text-sm text-gray-300 hover:text-white px-2 py-1 rounded hover:bg-gray-700 transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            Logout
          </button>
        </div>
      </header>

      <main id="main-content" ref={mainRef} tabIndex={-1} className="flex-1 overflow-hidden focus:outline-none">
        <Outlet />
      </main>
    </div>
  );
}
