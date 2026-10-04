import { useId, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * Shared left filter panel for list pages (issue #957).
 *
 * Layout and labels only: every value, handler, and the decision to show the
 * Clear control come from the page, which keeps its own state, query
 * parameters, and persistence. Topologies and Reservations adopt it the same
 * way with their own state.
 *
 * Each layout choice is one constant below so it is easy to change:
 * - PANEL_WIDTH: the column width beside the table (DevicePage's aside width).
 * - STACK_BREAKPOINT: below this the panel stacks above the table.
 * - PANEL_STICKY: keeps the panel in view while a long table scrolls.
 */
const PANEL_WIDTH = "lg:w-60";
const STACK_BREAKPOINT_ROW = "lg:flex-row lg:items-start";
const PANEL_STICKY = "lg:sticky lg:top-6";

const CONTROL_CLASS =
  "w-full text-sm font-normal text-gray-900 border border-gray-300 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500";

/**
 * Two columns: the filter panel, then the list. The panel comes first in the
 * DOM, so tab order runs panel then table; on a narrow viewport it stacks
 * above the list.
 */
export function ListFilterLayout({
  panel,
  children,
}: {
  panel: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className={cn("flex flex-col gap-4", STACK_BREAKPOINT_ROW)}>
      {panel}
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  );
}

export function ListFilterPanel({
  searchLabel,
  searchPlaceholder,
  searchValue,
  onSearchChange,
  showClear,
  onClear,
  children,
}: {
  /** Visible label and accessible name of the search box. */
  searchLabel: string;
  searchPlaceholder?: string;
  searchValue: string;
  onSearchChange: (value: string) => void;
  /** The page decides when anything is active. */
  showClear: boolean;
  onClear: () => void;
  /** Labeled filter controls, stacked under the search box. */
  children?: ReactNode;
}) {
  const searchId = useId();
  return (
    <section
      aria-label="Filters"
      className={cn("w-full shrink-0", PANEL_WIDTH, PANEL_STICKY)}
    >
      <div className="bg-white rounded-lg border border-gray-200 p-4 flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor={searchId} className="text-xs font-medium text-gray-500">
            {searchLabel}
          </label>
          <input
            id={searchId}
            type="text"
            placeholder={searchPlaceholder}
            value={searchValue}
            onChange={(e) => onSearchChange(e.target.value)}
            className={cn(CONTROL_CLASS, "px-3 py-2")}
          />
        </div>
        {children}
        {showClear && (
          <button
            type="button"
            onClick={onClear}
            className="w-full px-3 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
          >
            Clear filters
          </button>
        )}
      </div>
    </section>
  );
}

/**
 * A labeled select for the panel. An explicit htmlFor/id pair, not a wrapping
 * label: a wrapping label's accessible name would include the selected
 * option's text.
 */
export function FilterSelect({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-gray-500">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(CONTROL_CLASS, "px-2 py-2")}
      >
        {children}
      </select>
    </div>
  );
}
