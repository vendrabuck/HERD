import toast from "react-hot-toast";
import { APPLY_JOBS_PAGE_SIZE, useApplyJobs, useCancelApplyJob } from "@/api/deviceConfigJobs";
import type { ApplyJob } from "@/api/deviceConfigJobs";
import { LoadError, LoadErrorNotice } from "@/components/ui/LoadError";
import { PageCapNotice } from "@/components/ui/PageCapNotice";
import { errorDetail } from "@/lib/errors";
import { loadState, noItems } from "@/lib/loadState";

const STATUS_COLORS: Record<string, string> = {
  pending: "text-yellow-700",
  running: "text-blue-700",
  success: "text-green-700",
  failed: "text-red-700",
  skipped: "text-gray-500",
  cancelled: "text-gray-500",
};

export function ApplyJobsPanel({ deviceId }: { deviceId: string }) {
  // One page on each 10-second poll, never a walk of every page (issue #1590);
  // PageCapNotice says when more jobs exist than the page holds.
  const query = useApplyJobs(deviceId, 0, APPLY_JOBS_PAGE_SIZE);
  const cancelJob = useCancelApplyJob(deviceId);
  const state = loadState(query, noItems);

  // Hidden only on an answered empty list: a failed read shows LoadError, and a
  // failed poll over an empty list shows the heading and the refresh notice.
  if (state === "empty" && !query.isError) {
    return null;
  }

  const page = state === "loaded" ? query.data : undefined;
  const jobs = page?.items ?? [];

  const handleCancel = async (job: ApplyJob) => {
    try {
      await cancelJob.mutateAsync(job.id);
      toast.success("Cancelled");
    } catch (err: unknown) {
      toast.error(errorDetail(err, "Cancel failed"));
    }
  };

  // The table stays an unwrapped later sibling of the heading paragraph: the
  // e2e suite finds the rows as the heading's following-sibling table.
  return (
    <div className="mt-4 border-t border-gray-200 pt-3">
      <p className="text-xs font-medium text-gray-700 mb-2">Scheduled applies</p>
      {state === "error" ? (
        <LoadError
          what="scheduled applies"
          error={query.error}
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        />
      ) : (
        <>
          <LoadErrorNotice
            what="scheduled applies"
            query={query}
            isEmpty={noItems}
            className="mb-2"
          />
          {(state === "loading" || state === "loaded") && (
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
          )}
          {page && (
            <PageCapNotice
              shown={page.items.length}
              total={page.total}
              noun="scheduled applies"
              order="latest scheduled first"
              className="mt-2"
            />
          )}
        </>
      )}
    </div>
  );
}
