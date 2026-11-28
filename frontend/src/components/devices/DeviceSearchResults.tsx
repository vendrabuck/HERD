import type { useDeviceSearch } from "@/api/inventory";
import { LoadError } from "@/components/ui/LoadError";
import { MoreMatchesNotice } from "@/components/ui/MoreMatchesNotice";
import { cn } from "@/lib/cn";
import { loadState, noItems } from "@/lib/loadState";
import type { Device } from "@/types/device.types";

/**
 * The ONE result list under a device typeahead (issue #1590), used by
 * `DeviceSearchPicker` and the Connections page's filter and single-pair
 * searches. In its four load states it shows: nothing while the first answer
 * is on its way; `LoadError` (`Could not load matching devices.`, with Retry)
 * when the search failed; `No devices match` when the search answered with no
 * rows; else one button per device, named by the device, and under them
 * `MoreMatchesNotice` when the server holds more matches than the page. So a
 * failed search never reads as a search with no match, and a match past the
 * page is never silently missing. While a new term is on its way the previous
 * term's rows stay (`keepPreviousData`), but the notice is hidden: its count
 * belongs to the previous term, not the text now in the box.
 */
export function DeviceSearchResults({
  query,
  onPick,
  className,
  itemClassName,
}: {
  query: ReturnType<typeof useDeviceSearch>["query"];
  onPick: (device: Device) => void;
  /** Placement and size of the dropdown box. */
  className?: string;
  /** Text size and padding of each result button. */
  itemClassName?: string;
}) {
  const state = loadState(query, noItems);
  if (state === "loading") return null;
  const box = cn(
    "absolute z-20 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg overflow-y-auto",
    className,
  );
  if (state === "error") {
    return (
      <div className={box}>
        <LoadError
          what="matching devices"
          error={query.error}
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
          className="m-2"
        />
      </div>
    );
  }
  if (state === "empty") {
    return (
      <div className={box}>
        <p className="px-3 py-2 text-xs text-gray-500">No devices match</p>
      </div>
    );
  }
  const page = query.data!;
  return (
    <div className={box}>
      {page.items.map((d) => (
        <button
          key={d.id}
          type="button"
          onClick={() => onPick(d)}
          className={cn("block w-full text-left hover:bg-gray-100", itemClassName)}
        >
          {d.name}
        </button>
      ))}
      {!query.isPlaceholderData && (
        <MoreMatchesNotice
          shown={page.items.length}
          total={page.total}
          noun="devices"
          className="px-3 py-2 border-t border-gray-100"
        />
      )}
    </div>
  );
}
