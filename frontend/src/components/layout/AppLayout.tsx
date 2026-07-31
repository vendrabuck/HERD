import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate, useLocation } from "react-router-dom";
import { useCurrentUser, useLogout } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { HelpCircle } from "lucide-react";
import { NotificationBell } from "@/components/NotificationBell";

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
  const [adminOpen, setAdminOpen] = useState(false);

  useEffect(() => {
    useAuthStore.getState().setUser(user ?? null);
    if (user) usePreferencesStore.getState().load();
  }, [user]);

  const handleLogout = async () => {
    await logout.mutateAsync();
    usePreferencesStore.getState().clear();
    navigate("/login");
  };

  const isAdmin = user?.role === "admin" || user?.role === "superadmin";
  const adminActive = location.pathname.startsWith("/admin");

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-gray-100">
      <header className="flex items-center justify-between px-4 py-2 bg-gray-900 text-white shrink-0 border-b border-gray-800">
        <div className="flex items-center gap-4">
          <span className="font-bold text-lg tracking-tight">HERD</span>
          <nav className="flex gap-1">
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
            {isAdmin && (
              <div
                className="relative"
                onMouseEnter={() => setAdminOpen(true)}
                onMouseLeave={() => setAdminOpen(false)}
              >
                <button
                  type="button"
                  className={`text-sm px-3 py-1 rounded transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                    adminActive
                      ? "bg-gray-700 text-white"
                      : "text-gray-300 hover:text-white hover:bg-gray-800"
                  }`}
                >
                  Administration
                </button>
                {adminOpen && (
                  <div className="absolute left-0 top-full bg-gray-800 rounded shadow-lg py-1 min-w-40 z-50">
                    <NavLink
                      to="/admin/add-device"
                      onClick={() => setAdminOpen(false)}
                      className={({ isActive }) =>
                        `block px-4 py-2 text-sm transition-colors ${
                          isActive
                            ? "bg-gray-700 text-white"
                            : "text-gray-300 hover:text-white hover:bg-gray-700"
                        }`
                      }
                    >
                      Add Device
                    </NavLink>
                    <NavLink
                      to="/admin/groups"
                      onClick={() => setAdminOpen(false)}
                      className={({ isActive }) =>
                        `block px-4 py-2 text-sm transition-colors ${
                          isActive
                            ? "bg-gray-700 text-white"
                            : "text-gray-300 hover:text-white hover:bg-gray-700"
                        }`
                      }
                    >
                      User Groups
                    </NavLink>
                    <NavLink
                      to="/admin/device-groups"
                      onClick={() => setAdminOpen(false)}
                      className={({ isActive }) =>
                        `block px-4 py-2 text-sm transition-colors ${
                          isActive
                            ? "bg-gray-700 text-white"
                            : "text-gray-300 hover:text-white hover:bg-gray-700"
                        }`
                      }
                    >
                      Device Groups
                    </NavLink>
                    <NavLink
                      to="/admin/connections"
                      onClick={() => setAdminOpen(false)}
                      className={({ isActive }) =>
                        `block px-4 py-2 text-sm transition-colors ${
                          isActive
                            ? "bg-gray-700 text-white"
                            : "text-gray-300 hover:text-white hover:bg-gray-700"
                        }`
                      }
                    >
                      Connections
                    </NavLink>
                    <NavLink
                      to="/admin/drivers"
                      onClick={() => setAdminOpen(false)}
                      className={({ isActive }) =>
                        `block px-4 py-2 text-sm transition-colors ${
                          isActive
                            ? "bg-gray-700 text-white"
                            : "text-gray-300 hover:text-white hover:bg-gray-700"
                        }`
                      }
                    >
                      Drivers
                    </NavLink>
                    <NavLink
                      to="/admin/grants"
                      onClick={() => setAdminOpen(false)}
                      className={({ isActive }) =>
                        `block px-4 py-2 text-sm transition-colors ${
                          isActive
                            ? "bg-gray-700 text-white"
                            : "text-gray-300 hover:text-white hover:bg-gray-700"
                        }`
                      }
                    >
                      Grants
                    </NavLink>
                    <NavLink
                      to="/admin/users"
                      onClick={() => setAdminOpen(false)}
                      className={({ isActive }) =>
                        `block px-4 py-2 text-sm transition-colors ${
                          isActive
                            ? "bg-gray-700 text-white"
                            : "text-gray-300 hover:text-white hover:bg-gray-700"
                        }`
                      }
                    >
                      Users
                    </NavLink>
                  </div>
                )}
              </div>
            )}
          </nav>
        </div>
        <div className="flex items-center gap-3">
          <a
            href="https://github.com/vendrabuck/HERD/blob/main/docs/USER_GUIDE.md"
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
            <span className="text-sm text-gray-300">{user.username}</span>
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

      <div className="flex-1 overflow-hidden">
        <Outlet />
      </div>
    </div>
  );
}
