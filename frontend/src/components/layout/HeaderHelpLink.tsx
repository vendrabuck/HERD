import { CircleHelp } from "lucide-react";
import { MANUAL_URL } from "@/lib/links";

// The header's way to the published user manual (issue #960). The manual is
// a separate site, so it opens in a new tab with no opener or referrer; the
// icon has no text of its own, so the link's accessible name is "Help".
// Sized and styled like the notification bell and the Logout button beside it.
export function HeaderHelpLink() {
  return (
    <a
      href={MANUAL_URL}
      target="_blank"
      rel="noopener noreferrer"
      aria-label="Help"
      title="Help"
      className="inline-flex items-center text-gray-300 hover:text-white px-2 py-1 rounded hover:bg-gray-700 transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500"
    >
      <CircleHelp aria-hidden="true" className="w-5 h-5" />
    </a>
  );
}
