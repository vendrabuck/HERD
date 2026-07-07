import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { useAuthStore } from "@/stores/authStore";
import {
  usePaginatedDrivers,
  useCreateDriver,
  useDeleteDriver,
  useDownloadDriver,
} from "@/api/drivers";
import { useAIStatus } from "@/api/ai";
import { RecipeDraftPanel } from "@/components/admin/RecipeDraftPanel";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import type { ConnectionType } from "@/types/driver.types";

const CONNECTION_TYPES: ConnectionType[] = [
  "Management",
  "Layer 1 Switch",
  "Layer 2 Switch",
  "Layer 3 Switch",
  "Hypervisor",
];

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function DriversPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const [skip, setSkip] = useState(0);
  const limit = 50;
  const { data, isLoading } = usePaginatedDrivers(skip, limit);
  const drivers = data?.items;
  const total = data?.total ?? 0;
  const createDriver = useCreateDriver();
  const deleteDriver = useDeleteDriver();
  const downloadDriver = useDownloadDriver();

  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  const aiStatus = useAIStatus();
  const recipeAuthoringOn = Boolean(
    aiStatus.data?.enabled && aiStatus.data?.recipe_authoring,
  );

  const [showUpload, setShowUpload] = useState(false);
  const [showRecipePanel, setShowRecipePanel] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [uploadName, setUploadName] = useState("");
  const [uploadDesc, setUploadDesc] = useState("");
  const [uploadConnectionType, setUploadConnectionType] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  if (!user || !isAdmin) return null;

  const handleUpload = async () => {
    if (!uploadName.trim()) {
      toast.error("Name is required");
      return;
    }
    if (!uploadConnectionType) {
      toast.error("Connection type is required");
      return;
    }
    if (!uploadFile) {
      toast.error("File is required");
      return;
    }
    try {
      await createDriver.mutateAsync({
        name: uploadName.trim(),
        description: uploadDesc.trim() || undefined,
        connection_type: uploadConnectionType,
        file: uploadFile,
      });
      toast.success("Driver uploaded");
      setShowUpload(false);
      setUploadName("");
      setUploadDesc("");
      setUploadConnectionType("");
      setUploadFile(null);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to upload driver";
      toast.error(msg);
    }
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      await deleteDriver.mutateAsync(deleteId);
      toast.success("Driver deleted");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete driver";
      toast.error(msg);
    }
    setDeleteId(null);
  };

  const handleDownload = async (id: string, filename: string) => {
    try {
      const blob = await downloadDriver.mutateAsync(id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast.error("Failed to download driver");
    }
  };

  const closeUploadModal = () => {
    setShowUpload(false);
    setUploadName("");
    setUploadDesc("");
    setUploadConnectionType("");
    setUploadFile(null);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">Drivers</h2>
          <div className="flex gap-2">
            {recipeAuthoringOn && (
              <button
                onClick={() => setShowRecipePanel(true)}
                className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
              >
                Draft with AI
              </button>
            )}
            <button
              onClick={() => setShowUpload(true)}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
            >
              Upload Driver
            </button>
          </div>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading ? (
            <p className="text-sm text-gray-500 px-4 py-4">Loading drivers...</p>
          ) : !drivers || drivers.length === 0 ? (
            <p className="text-sm text-gray-500 px-4 py-4">No drivers found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[700px] text-sm text-left">
                <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Name</th>
                    <th className="px-4 py-3">Connection Type</th>
                    <th className="px-4 py-3">Description</th>
                    <th className="px-4 py-3">Filename</th>
                    <th className="px-4 py-3">Size</th>
                    <th className="px-4 py-3">Uploaded By</th>
                    <th className="px-4 py-3">Date</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {drivers.map((driver) => (
                    <tr key={driver.id} className="hover:bg-gray-50">
                      <td className="px-4 py-3 font-medium text-gray-900">
                        {driver.name}
                      </td>
                      <td className="px-4 py-3 text-gray-600">
                        {driver.connection_type}
                      </td>
                      <td className="px-4 py-3 text-gray-600">
                        {driver.description || "-"}
                      </td>
                      <td className="px-4 py-3 text-gray-600 font-mono text-xs">
                        {driver.filename}
                      </td>
                      <td className="px-4 py-3 text-gray-600">
                        {formatBytes(driver.size_bytes)}
                      </td>
                      <td className="px-4 py-3 text-gray-600">
                        {driver.uploaded_by}
                      </td>
                      <td className="px-4 py-3 text-gray-500">
                        {new Date(driver.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex gap-2">
                          <button
                            onClick={() =>
                              handleDownload(driver.id, driver.filename)
                            }
                            className="text-xs text-blue-600 hover:text-blue-800"
                          >
                            Download
                          </button>
                          <button
                            onClick={() => setDeleteId(driver.id)}
                            className="text-xs text-red-600 hover:text-red-800"
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>
      </div>

      <Modal
        open={showUpload}
        onClose={closeUploadModal}
        title="Upload Driver"
        className="max-w-md"
      >
        <div className="space-y-4">
          <div>
            <label
              htmlFor="driver-name"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Name
            </label>
            <input
              id="driver-name"
              type="text"
              value={uploadName}
              onChange={(e) => setUploadName(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label
              htmlFor="driver-desc"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Description
            </label>
            <textarea
              id="driver-desc"
              value={uploadDesc}
              onChange={(e) => setUploadDesc(e.target.value)}
              rows={2}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label
              htmlFor="driver-connection-type"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Connection Type
            </label>
            <select
              id="driver-connection-type"
              value={uploadConnectionType}
              onChange={(e) => setUploadConnectionType(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Select connection type</option>
              {CONNECTION_TYPES.map((ct) => (
                <option key={ct} value={ct}>
                  {ct}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor="driver-file"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              File (.zip or .tar.gz)
            </label>
            <input
              id="driver-file"
              ref={fileRef}
              type="file"
              accept=".zip,.tar.gz"
              onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              className="text-sm text-gray-500 file:mr-2 file:py-1 file:px-3 file:rounded file:border-0 file:text-sm file:bg-gray-100 file:text-gray-700 hover:file:bg-gray-200"
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={closeUploadModal}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleUpload}
              disabled={createDriver.isPending}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {createDriver.isPending ? "Uploading..." : "Upload"}
            </button>
          </div>
        </div>
      </Modal>

      <RecipeDraftPanel open={showRecipePanel} onClose={() => setShowRecipePanel(false)} />

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete Driver"
        description="Are you sure you want to delete this driver? This cannot be undone. Deletion will fail if templates reference it."
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  );
}
