import { useState } from "react";
import { CreateDeviceForm } from "@/components/admin/CreateDeviceForm";

export function AddDevicePage() {
  const [showAddDevice, setShowAddDevice] = useState(false);

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-4xl mx-auto px-6 py-8">
        <section aria-labelledby="add-device-heading">
          <button
            onClick={() => setShowAddDevice((v) => !v)}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            {showAddDevice ? "Hide Form" : "Add Device"}
          </button>
          {showAddDevice && (
            <div className="bg-white rounded-lg border border-gray-200 p-6 mt-4">
              <CreateDeviceForm />
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
