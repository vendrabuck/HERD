import { AUTHOR_CREDIT_PREFIX, AUTHOR_LINKEDIN_URL, AUTHOR_NAME } from "@/lib/links";
import { cn } from "@/lib/cn";

// The author credit: one quiet line, the name linking to the author's LinkedIn
// profile. The profile is a separate site, so the link opens in a new tab with
// no opener or referrer, and names itself with aria-label, as HeaderHelpLink
// does. The label adds where the link goes and that it opens a new tab, and
// starts with the visible name, so speech input finds it by what it shows
// (WCAG 2.5.3). Pages place the credit after their form, so it never comes
// before a sign-in control in tab order.
export function AuthorCredit({ className }: { className?: string }) {
  return (
    <p data-testid="author-credit" className={cn("text-xs text-gray-500", className)}>
      {AUTHOR_CREDIT_PREFIX}{" "}
      <a
        href={AUTHOR_LINKEDIN_URL}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`${AUTHOR_NAME} on LinkedIn (opens in a new tab)`}
        className="font-medium text-blue-600 hover:text-blue-700 hover:underline rounded transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        {AUTHOR_NAME}
      </a>
    </p>
  );
}
