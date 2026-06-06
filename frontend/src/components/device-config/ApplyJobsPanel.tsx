import toast from "react-hot-toast";
import { useApplyJobs, useCancelApplyJob } from "@/api/deviceConfigJobs";
import type { ApplyJob } from "@/api/deviceConfigJobs";

const STATUS_COLORS: Record<string, string> = {
  pending: "text-yellow-700",
  running: "text-blue-700",
  success: "text-green-700",
  failed: "text-red-700",
  skipped: "text-gray-500",
  cancelled: "text-gray-500",
};

export function ApplyJobsPanel({ deviceId }: { deviceId: string }) {
  const { data, isLoading } = useApplyJobs(deviceId);
  const cancelJob = useCancelApplyJob(deviceId);

  const jobs = data?.items ?? [];
  if (!isLoading && jobs.length === 0) {
    return null;
  }

  const handleCancel = async (job: ApplyJob) => {
    try {
      await cancelJob.mutateAsync(job.id);
      toast.success("Cancelled");
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? "Cancel failed");
    }
  };

  return (
    <div className="mt-4 border-t border-gray-200 pt-3">
      <p className="text-xs font-medium text-gray-700 mb-2">Scheduled applies</p>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-gray-500 uppercase border-b border-gray-100">
            <th className="py-1">When</th>
            <th className="py-1">Status</th>
            <th className="py-1">Author</th>
            <th className="py-1">Run</th>
            <th className="py-1 text-right"></th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id} className="border-b border-gray-50">
              <td className="py-1 text-gray-700">
                {new Date(j.scheduled_for).toLocaleString()}
              </td>
              <td className={`py-1 ${STATUS_COLORS[j.status] ?? "text-gray-700"}`}>
                {j.status}
                {j.error && <span className="ml-1 text-gray-500">({j.error})</span>}
              </td>
              <td className="py-1 text-gray-600">{j.author_name || j.created_by.slice(0, 8)}</td>
              <td className="py-1 text-gray-500">
                {j.run_id ? j.run_id.slice(0, 8) : "-"}
              </td>
              <td className="py-1 text-right">
                {j.status === "pending" && (
                  <button
                    type="button"
                    onClick={() => handleCancel(j)}
                    className="text-xs text-red-600 hover:text-red-800 px-2 py-0.5 rounded hover:bg-red-50"
                  >
                    Cancel
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
