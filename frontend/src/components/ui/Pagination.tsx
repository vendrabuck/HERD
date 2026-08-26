import { useId } from "react";

interface PaginationProps {
  total: number;
  skip: number;
  limit: number;
  onPageChange: (skip: number) => void;
  pageSizeOptions?: number[];
  onPageSizeChange?: (limit: number) => void;
}

export function Pagination({
  total,
  skip,
  limit,
  onPageChange,
  pageSizeOptions,
  onPageSizeChange,
}: PaginationProps) {
  // useId must run every render, before any early return, so two
  // selector-bearing paginations on one page never collide on id.
  const pageSizeSelectId = useId();

  const hasPageSizeSelector = pageSizeOptions !== undefined && onPageSizeChange !== undefined;

  // A user with fewer rows than the current page size still needs a way to
  // change it, so the bar renders whenever the selector is present, not just
  // when there is more than one page. An empty result set stays hidden
  // either way: there is nothing to page through or size.
  if (total === 0) return null;
  if (total <= limit && !hasPageSizeSelector) return null;

  const currentPage = Math.floor(skip / limit) + 1;
  const totalPages = Math.ceil(total / limit);
  const from = skip + 1;
  const to = Math.min(skip + limit, total);

  // The persisted limit can be any value the backend accepted (1..500), not
  // necessarily one of the offered options, so fold it in and sort ascending
  // rather than let the select silently fall back to the first option while
  // the page still shows the real (unlisted) row count.
  const selectOptions = hasPageSizeSelector
    ? Array.from(new Set([...pageSizeOptions, limit])).sort((a, b) => a - b)
    : [];

  const navControls = (
    <div className="flex items-center gap-2">
      <button
        onClick={() => onPageChange(skip - limit)}
        disabled={skip === 0}
        className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        Prev
      </button>
      <span className="text-sm text-gray-500">
        Page {currentPage} of {totalPages}
      </span>
      <button
        onClick={() => onPageChange(skip + limit)}
        disabled={skip + limit >= total}
        className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        Next
      </button>
    </div>
  );

  return (
    <div className="flex items-center justify-between px-4 py-3 border-t border-gray-200 bg-white">
      <span className="text-sm text-gray-500">
        Showing {from}-{to} of {total}
      </span>
      {hasPageSizeSelector ? (
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1.5">
            <label htmlFor={pageSizeSelectId} className="text-sm text-gray-500">
              Rows per page
            </label>
            <select
              id={pageSizeSelectId}
              value={limit}
              onChange={(e) => onPageSizeChange(Number(e.target.value))}
              className="text-sm border border-gray-300 rounded px-2 py-1 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            >
              {selectOptions.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </div>
          {navControls}
        </div>
      ) : (
        navControls
      )}
    </div>
  );
}
