import { cn } from "@/lib/cn";

/** A single shimmering placeholder bar. animate-pulse is the only loading motion. */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("h-6 bg-gray-100 rounded animate-pulse", className)} />;
}

/** A stack of skeleton bars for loading table bodies. */
export function SkeletonRows({ rows = 3 }: { rows?: number }) {
  return (
    <div className="p-4 space-y-2">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} />
      ))}
    </div>
  );
}
