// Shared assertions for the author credit (AuthorCredit) on every surface that
// shows it: the sign-in, registration, and config sign-in pages and About.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect } from "vitest";

// Literals on purpose, as HeaderHelpLink.test.tsx pins the manual's address:
// a change to the wording or the profile URL must show up as a test change.
export const AUTHOR_CREDIT_VISIBLE_TEXT = "Designed and built by Lane Nelms";
export const AUTHOR_LINK_NAME = "Lane Nelms on LinkedIn (opens in a new tab)";
export const AUTHOR_LINKEDIN_HREF = "https://www.linkedin.com/in/lane-nelms/";

/** The text a sighted reader sees, whitespace collapsed as a browser shows it. */
function visibleText(element: HTMLElement): string {
  return (element.textContent ?? "").replace(/\s+/g, " ").trim();
}

/**
 * Assert the page shows exactly one author credit, with the exact visible
 * text, a link with the intended accessible name, the exact href, and a new
 * tab without an opener or referrer. Returns the link.
 */
export function expectAuthorCredit(): HTMLElement {
  const credits = screen.getAllByTestId("author-credit");
  expect(credits).toHaveLength(1);
  const credit = credits[0];
  expect(visibleText(credit)).toBe(AUTHOR_CREDIT_VISIBLE_TEXT);
  const link = within(credit).getByRole("link", { name: AUTHOR_LINK_NAME });
  expect(visibleText(link)).toBe("Lane Nelms");
  expect(link).toHaveAttribute("href", AUTHOR_LINKEDIN_HREF);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  return link;
}

/**
 * Tab from the top of the document until the author credit link has focus and
 * return every element that had focus on the way, in order (the link last).
 * Fails if the link is never reached within `limit` presses.
 */
export async function tabToAuthorCredit(limit = 20): Promise<Element[]> {
  const link = screen.getByRole("link", { name: AUTHOR_LINK_NAME });
  const user = userEvent.setup();
  const order: Element[] = [];
  for (let i = 0; i < limit; i += 1) {
    await user.tab();
    const active = document.activeElement;
    if (active === null || active === document.body) break;
    order.push(active);
    if (active === link) return order;
  }
  throw new Error(`the author credit link was not reached by Tab: ${order.length} stops`);
}

/**
 * The structure that stacks the credit under a sign-in card: the link sits in
 * a footer that directly follows the card (`cardText` is text inside it), in a
 * page wrapper that is a flex column. jsdom has no layout, so this is as far as
 * a unit test reaches; the e2e helper `assert_author_credit_under_card`
 * measures the real boxes.
 */
export function expectCreditStackedUnderCard(link: HTMLElement, cardText: string): void {
  const footer = link.closest("footer");
  expect(footer).not.toBeNull();
  const card = footer!.previousElementSibling as HTMLElement | null;
  expect(card).not.toBeNull();
  expect(card).toHaveTextContent(cardText);
  expect(card!.className.split(/\s+/)).toContain("max-w-sm");
  const page = footer!.parentElement!;
  expect(page.className.split(/\s+/)).toEqual(expect.arrayContaining(["flex", "flex-col"]));
}
