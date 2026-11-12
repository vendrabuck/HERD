import { useEffect, useState } from "react";
import { titleForPath } from "@/lib/routeTitles";

/**
 * Sets `document.title` from `lib/routeTitles.ts` for the current pathname
 * (issue #1317). A path with no entry keeps the title the document had when
 * the hook first mounted (the index.html title), and unmounting restores it,
 * so the login page never inherits the last app page's title.
 */
export function useRouteTitle(pathname: string): void {
  const [initialTitle] = useState(() => document.title);
  useEffect(() => {
    document.title = titleForPath(pathname) ?? initialTitle;
    return () => {
      document.title = initialTitle;
    };
  }, [pathname, initialTitle]);
}
