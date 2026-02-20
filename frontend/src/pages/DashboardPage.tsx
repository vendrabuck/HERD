import { useNavigate } from "react-router-dom";
import { useCurrentUser, useLogout } from "@/api/auth";
import { EquipmentBrowser } from "@/components/equipment-browser/EquipmentBrowser";
import { TopologyEditor } from "@/components/topology-editor/TopologyEditor";
import { ReservationPanel } from "@/components/reservations/ReservationPanel";

export function DashboardPage() {
  const { data: user } = useCurrentUser();
  const logout = useLogout();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout.mutateAsync();
    navigate("/login");
  };

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-gray-100">
      {/* Top nav */}
      <header className="flex items-center justify-between px-4 py-2 bg-gray-900 text-white shrink-0">
        <div className="flex items-center gap-3">
          <span className="font-bold text-lg tracking-tight">HERD</span>
          <span className="text-gray-400 text-xs">Hardware Environment Replication and Deployment</span>
        </div>
        <div className="flex items-center gap-3">
          {user && (
            <span className="text-sm text-gray-300">
              {user.username}
            </span>
          )}
          <button
            onClick={handleLogout}
            disabled={logout.isPending}
            className="text-sm text-gray-400 hover:text-white px-2 py-1 rounded hover:bg-gray-700 transition-colors"
          >
            Logout
          </button>
        </div>
      </header>

      {/* Main content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left panel: Equipment Browser */}
        <div className="w-56 shrink-0 flex flex-col overflow-hidden">
          <EquipmentBrowser />
        </div>

        {/* Center: Topology Editor + Reservation Panel */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Topology editor takes most vertical space */}
          <div className="flex-1 overflow-hidden">
            <TopologyEditor />
          </div>

          {/* Reservation panel at the bottom */}
          <ReservationPanel />
        </div>
      </div>
    </div>
  );
}
