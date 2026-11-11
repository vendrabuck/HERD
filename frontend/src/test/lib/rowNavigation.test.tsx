import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { RESERVATION_NAME_PURPOSE_MAX, reservationOpenLabel, rowClick } from "@/lib/rowNavigation";
import { ADMIN_NAV, visibleAdminNav } from "@/lib/adminNav";

// Issue #1317: the row's mouse handler must ignore any click that started on
// an interactive child, and must still fire for a click on plain row content.
function Row({ open }: { open: () => void }) {
  return (
    <table>
      <tbody>
        <tr data-testid="row" onClick={rowClick(open)}>
          <td>
            plain <span>nested text</span>
          </td>
          <td>
            <a href="/x">
              link <span>inner</span>
            </a>
          </td>
          <td>
            <button type="button">
              action <svg data-testid="icon" />
            </button>
          </td>
          <td>
            <input type="checkbox" aria-label="pick" />
          </td>
          <td>
            <label>
              labelled <input type="radio" aria-label="choice" />
            </label>
          </td>
          <td>
            <select aria-label="sel">
              <option>a</option>
            </select>
          </td>
          <td>
            <div role="button" tabIndex={0}>
              fake button
            </div>
          </td>
          <td>
            <span role="switch" aria-checked="false" tabIndex={0}>
              a switch
            </span>
            <span role="menuitem" tabIndex={-1}>
              a menuitem
            </span>
            <span role="tab" tabIndex={0}>
              a tab
            </span>
            <span role="combobox" aria-expanded="false" tabIndex={0}>
              a combobox
            </span>
            <span role="option" aria-selected="false">
              an option
            </span>
            <span contentEditable suppressContentEditableWarning>
              editable
            </span>
            <span role="link" tabIndex={0}>
              fake link
            </span>
            <span role="checkbox" aria-checked="false" tabIndex={0}>
              fake checkbox
            </span>
          </td>
        </tr>
      </tbody>
    </table>
  );
}

describe("rowClick (issue #1317)", () => {
  it("fires for a click on the row or on plain, non-interactive content", () => {
    const open = vi.fn();
    render(<Row open={open} />);
    fireEvent.click(screen.getByTestId("row"));
    fireEvent.click(screen.getByText("plain"));
    fireEvent.click(screen.getByText("nested text"));
    expect(open).toHaveBeenCalledTimes(3);
  });

  it.each([
    ["a link", () => screen.getByText("link")],
    ["an element nested in a link", () => screen.getByText("inner")],
    ["a button", () => screen.getByText("action")],
    ["an icon inside a button", () => screen.getByTestId("icon")],
    ["a checkbox", () => screen.getByRole("checkbox", { name: "pick" })],
    ["a label", () => screen.getByText("labelled")],
    ["a select", () => screen.getByRole("combobox", { name: "sel" })],
    ["a role=button element", () => screen.getByText("fake button")],
    ["a role=link element", () => screen.getByText("fake link")],
    ["a role=checkbox element", () => screen.getByText("fake checkbox")],
    ["a role=switch element", () => screen.getByText("a switch")],
    ["a role=menuitem element", () => screen.getByText("a menuitem")],
    ["a role=tab element", () => screen.getByText("a tab")],
    ["a role=combobox element", () => screen.getByText("a combobox")],
    ["a role=option element", () => screen.getByText("an option")],
    ["a contenteditable element", () => screen.getByText("editable")],
  ])("ignores a click that started on %s", (_name, target) => {
    const open = vi.fn();
    render(<Row open={open} />);
    fireEvent.click(target());
    expect(open).not.toHaveBeenCalled();
  });
});

describe("visibleAdminNav (issue #1317)", () => {
  it.each(["admin", "superadmin"])("shows every entry to %s", (role) => {
    expect(visibleAdminNav(role)).toEqual([...ADMIN_NAV]);
  });

  it.each(["user", "Admin", "", null, undefined])("shows nothing to %s", (role) => {
    expect(visibleAdminNav(role)).toEqual([]);
  });
});

describe("reservationOpenLabel (issue #1317)", () => {
  it("starts with the visible short id and says what the button does", () => {
    expect(reservationOpenLabel("1a2b3c4d", null)).toBe("1a2b3c4d, open reservation");
    expect(reservationOpenLabel("1a2b3c4d", undefined)).toBe("1a2b3c4d, open reservation");
    expect(reservationOpenLabel("1a2b3c4d", "   ")).toBe("1a2b3c4d, open reservation");
  });

  it("appends the purpose with its whitespace collapsed", () => {
    expect(reservationOpenLabel("1a2b3c4d", "  fw   regression\n run ")).toBe(
      "1a2b3c4d, open reservation: fw regression run",
    );
  });

  it("keeps a purpose of exactly the maximum length whole", () => {
    const purpose = "p".repeat(RESERVATION_NAME_PURPOSE_MAX);
    expect(reservationOpenLabel("id", purpose)).toBe(`id, open reservation: ${purpose}`);
  });

  it("cuts a longer purpose to the maximum, ellipsis included, by code point", () => {
    const purpose = "\u{1F600}".repeat(RESERVATION_NAME_PURPOSE_MAX + 5);
    const label = reservationOpenLabel("id", purpose);
    const tail = label.slice("id, open reservation: ".length);
    expect(Array.from(tail)).toHaveLength(RESERVATION_NAME_PURPOSE_MAX);
    expect(tail.endsWith("...")).toBe(true);
    // No surrogate half survives the cut.
    expect(tail).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
  });
});
