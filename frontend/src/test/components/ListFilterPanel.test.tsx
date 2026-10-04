import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { FilterSelect, ListFilterLayout, ListFilterPanel } from "@/components/ui/ListFilterPanel";

function renderPanel(overrides: Partial<Parameters<typeof ListFilterPanel>[0]> = {}) {
  const props = {
    searchLabel: "Search things",
    searchPlaceholder: "Search things by name...",
    searchValue: "",
    onSearchChange: vi.fn(),
    showClear: false,
    onClear: vi.fn(),
    ...overrides,
  };
  render(
    <ListFilterPanel {...props}>
      <FilterSelect label="Kind" value="" onChange={vi.fn()}>
        <option value="">All</option>
        <option value="a">A</option>
      </FilterSelect>
    </ListFilterPanel>,
  );
  return props;
}

describe("ListFilterPanel", () => {
  it("is a region named Filters holding the search box and the child controls", () => {
    renderPanel();
    const region = screen.getByRole("region", { name: "Filters" });
    expect(within(region).getByRole("textbox", { name: "Search things" })).toBeInTheDocument();
    expect(within(region).getByRole("combobox", { name: "Kind" })).toBeInTheDocument();
  });

  it("labels the search box with its visible label and keeps the placeholder", () => {
    renderPanel();
    const input = screen.getByLabelText("Search things") as HTMLInputElement;
    expect(input.placeholder).toBe("Search things by name...");
    expect(screen.getByText("Search things").tagName).toBe("LABEL");
  });

  it("labels a FilterSelect by its own label only, not the selected option", () => {
    renderPanel();
    // The accessible name is exactly the label: a wrapping label would append "All".
    expect(screen.getByRole("combobox", { name: "Kind" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /All/ })).not.toBeInTheDocument();
  });

  it("shows the search value it is given and reports typing without owning state", () => {
    const props = renderPanel({ searchValue: "abc" });
    const input = screen.getByLabelText("Search things") as HTMLInputElement;
    expect(input.value).toBe("abc");
    fireEvent.change(input, { target: { value: "abcd" } });
    expect(props.onSearchChange).toHaveBeenCalledWith("abcd");
    // Controlled: the panel does not change its own value.
    expect(input.value).toBe("abc");
  });

  it("hides Clear filters when nothing is active", () => {
    renderPanel({ showClear: false });
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });

  it("shows Clear filters when something is active and calls onClear", () => {
    const props = renderPanel({ showClear: true });
    const button = screen.getByRole("button", { name: "Clear filters" });
    fireEvent.click(button);
    expect(props.onClear).toHaveBeenCalledTimes(1);
  });

  it("renders a FilterSelect's value and reports a change", () => {
    const onChange = vi.fn();
    render(
      <FilterSelect label="Kind" value="a" onChange={onChange}>
        <option value="">All</option>
        <option value="a">A</option>
      </FilterSelect>,
    );
    const select = screen.getByLabelText("Kind") as HTMLSelectElement;
    expect(select.value).toBe("a");
    fireEvent.change(select, { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith("");
  });
});

describe("ListFilterLayout", () => {
  it("puts the panel before the list in document order, so tab order is panel then table", () => {
    render(
      <ListFilterLayout
        panel={
          <ListFilterPanel
            searchLabel="Search things"
            searchValue=""
            onSearchChange={vi.fn()}
            showClear={false}
            onClear={vi.fn()}
          />
        }
      >
        <table>
          <tbody>
            <tr>
              <td>
                <button type="button">Row action</button>
              </td>
            </tr>
          </tbody>
        </table>
      </ListFilterLayout>,
    );
    const search = screen.getByLabelText("Search things");
    const rowAction = screen.getByRole("button", { name: "Row action" });
    expect(
      search.compareDocumentPosition(rowAction) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
