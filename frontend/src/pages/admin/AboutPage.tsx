import { RefreshCw } from "lucide-react";
import { APP_VERSION, APP_BUILD, APP_BUILD_DATE, sameRelease, buildsDiffer } from "@/lib/appVersion";
import { useServiceVersions } from "@/api/about";

function formatBuildDate(iso: string | null): string {
  if (!iso) return "-";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

export function AboutPage() {
  const rows = useServiceVersions();

  const handleRefresh = () => {
    for (const row of rows) {
      row.refetch();
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">About</h2>
          <button
            type="button"
            onClick={handleRefresh}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Refresh
          </button>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 p-4 mb-8">
          <h3 className="text-base font-semibold text-gray-900 mb-3">Frontend</h3>
          <dl className="grid grid-cols-3 gap-4 text-sm">
            <div>
              <dt className="text-xs uppercase text-gray-500 mb-1">Version</dt>
              <dd className="text-gray-900 font-medium">{APP_VERSION}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase text-gray-500 mb-1">Build</dt>
              <dd className="text-gray-900 font-medium font-mono text-xs">{APP_BUILD}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase text-gray-500 mb-1">Build date</dt>
              <dd className="text-gray-900 font-medium">{formatBuildDate(APP_BUILD_DATE)}</dd>
            </div>
          </dl>
        </div>

        <h3 className="text-base font-semibold text-gray-900 mb-3">Services</h3>
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[700px] text-sm text-left">
              <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                <tr>
                  <th className="px-4 py-3">Service</th>
                  <th className="px-4 py-3">Version</th>
                  <th className="px-4 py-3">Build</th>
                  <th className="px-4 py-3">Build date</th>
                  <th className="px-4 py-3">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {rows.map(({ service, data, isLoading, isError }) => {
                  const reachable = !isLoading && !isError && !!data;
                  const versionDiffers = reachable && !sameRelease(data.version, APP_VERSION);
                  const buildDiffers = reachable && buildsDiffer(data.build, APP_BUILD);
                  const skew = versionDiffers || buildDiffers;

                  return (
                    <tr
                      key={service.name}
                      className={`hover:bg-gray-50 ${skew ? "bg-amber-50" : ""}`}
                    >
                      <td className="px-4 py-3 font-medium text-gray-900">{service.label}</td>
                      <td className="px-4 py-3 text-gray-600">
                        {reachable ? (
                          <span className="inline-flex items-center gap-2">
                            <span>{data.version}</span>
                            {versionDiffers && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 font-medium">
                                differs
                              </span>
                            )}
                          </span>
                        ) : (
                          <span className="text-gray-400">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-gray-600">
                        {reachable ? (
                          <span className="inline-flex items-center gap-2">
                            <span className="font-mono text-xs">{data.build}</span>
                            {buildDiffers && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 font-medium">
                                differs
                              </span>
                            )}
                          </span>
                        ) : (
                          <span className="text-gray-400">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-gray-500">
                        {reachable ? (
                          formatBuildDate(data.build_date)
                        ) : (
                          <span className="text-gray-400">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {isLoading ? (
                          <span className="text-xs text-gray-400">Loading...</span>
                        ) : reachable ? (
                          <span className="text-xs font-medium text-green-700">reachable</span>
                        ) : (
                          <span className="text-xs font-medium text-red-600">unreachable</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
