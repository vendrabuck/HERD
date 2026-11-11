import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { rowClick } from "@/lib/rowNavigation";
import { ADMIN_NAV, visibleAdminNav } from "@/lib/adminNav";
import { titleForPath } from "@/lib/routeTitles";

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

describe("titleForPath (issue #1317)", () => {
  it.each([
    ["/inventory", "Inventory - HERD"],
    ["/inventory/abc", "Device - HERD"],
    ["/templates/new", "New template - HERD"],
    ["/templates/t-1", "Template - HERD"],
    ["/topology", "Topology - HERD"],
    ["/topology/abc", "Topology editor - HERD"],
    ["/reservations/calendar", "Reservation calendar - HERD"],
    ["/admin/groups/new", "New user group - HERD"],
    ["/admin/groups/g-1", "User group - HERD"],
    ["/admin/about", "About - HERD"],
  ])("titles %s as %s", (path, title) => {
    expect(titleForPath(path)).toBe(title);
  });

  it("returns null for a path with no route", () => {
    expect(titleForPath("/nowhere")).toBe(null);
    expect(titleForPath("/inventory/a/b")).toBe(null);
  });
});
