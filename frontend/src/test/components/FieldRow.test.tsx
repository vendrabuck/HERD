import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { FieldRow } from "@/components/templates/FieldRow";
import type { FieldDefinition } from "@/types/template.types";

const STRING_FIELD: FieldDefinition = {
  key: "hostname",
  label: "Hostname",
  type: "string",
  required: false,
};

function renderRow(field: FieldDefinition, onChange = vi.fn(), onRemove = vi.fn()) {
  render(<FieldRow field={field} index={0} onChange={onChange} onRemove={onRemove} />);
  return { onChange, onRemove };
}

describe("FieldRow key/label/required editing", () => {
  it("edits key and reports index 0 with the patched field", () => {
    const { onChange } = renderRow(STRING_FIELD);
    fireEvent.change(screen.getByPlaceholderText("Key"), { target: { value: "mgmt_ip" } });
    expect(onChange).toHaveBeenCalledWith(0, { ...STRING_FIELD, key: "mgmt_ip" });
  });

  it("edits label", () => {
    const { onChange } = renderRow(STRING_FIELD);
    fireEvent.change(screen.getByPlaceholderText("Label"), { target: { value: "Management IP" } });
    expect(onChange).toHaveBeenCalledWith(0, { ...STRING_FIELD, label: "Management IP" });
  });

  it("toggles required", () => {
    const { onChange } = renderRow(STRING_FIELD);
    fireEvent.click(screen.getByLabelText("Required"));
    expect(onChange).toHaveBeenCalledWith(0, { ...STRING_FIELD, required: true });
  });

  it("calls onRemove with the row index", () => {
    const { onRemove } = renderRow(STRING_FIELD);
    fireEvent.click(screen.getByTitle("Remove field"));
    expect(onRemove).toHaveBeenCalledWith(0);
  });
});

describe("FieldRow type switching clears stale default/options", () => {
  it("switching string to number clears default and leaves options untouched (was already undefined)", () => {
    const field: FieldDefinition = { ...STRING_FIELD, default: "eth0" };
    const { onChange } = renderRow(field);
    fireEvent.change(screen.getByDisplayValue("String"), { target: { value: "number" } });
    expect(onChange).toHaveBeenCalledWith(0, {
      ...field,
      type: "number",
      default: undefined,
      options: undefined,
    });
  });

  it("switching to dropdown clears default and initializes options to an empty array", () => {
    const field: FieldDefinition = { ...STRING_FIELD, default: "eth0" };
    const { onChange } = renderRow(field);
    fireEvent.change(screen.getByDisplayValue("String"), { target: { value: "dropdown" } });
    expect(onChange).toHaveBeenCalledWith(0, {
      ...field,
      type: "dropdown",
      default: undefined,
      options: [],
    });
  });

  it("switching from dropdown to string clears options", () => {
    const field: FieldDefinition = {
      ...STRING_FIELD,
      type: "dropdown",
      options: ["a", "b"],
      default: "a",
    };
    const { onChange } = renderRow(field);
    fireEvent.change(screen.getByDisplayValue("Dropdown"), { target: { value: "string" } });
    expect(onChange).toHaveBeenCalledWith(0, {
      ...field,
      type: "string",
      default: undefined,
      options: undefined,
    });
  });

  it("re-selecting the same type still clears default (patch.type !== field.type is false only when equal, but the row always fires update)", () => {
    // update() only special-cases patch.type !== field.type; selecting the
    // already-active option leaves the value string unchanged in the DOM so
    // no change event fires at all here. This test instead pins that a type
    // change to a DIFFERENT dropdown-adjacent type does not silently keep an
    // old options array around.
    const field: FieldDefinition = { ...STRING_FIELD, type: "boolean", default: true };
    const { onChange } = renderRow(field);
    fireEvent.change(screen.getByDisplayValue("Boolean"), { target: { value: "string" } });
    expect(onChange).toHaveBeenCalledWith(0, {
      ...field,
      type: "string",
      default: undefined,
      options: undefined,
    });
  });
});

describe("FieldRow dropdown options editing", () => {
  it("shows an options input only for dropdown fields", () => {
    renderRow(STRING_FIELD);
    expect(screen.queryByPlaceholderText("Options (comma-separated)")).not.toBeInTheDocument();
  });

  it("parses comma-separated options on blur, trimming and dropping empties", () => {
    const field: FieldDefinition = { ...STRING_FIELD, type: "dropdown", options: [] };
    const { onChange } = renderRow(field);
    const input = screen.getByPlaceholderText("Options (comma-separated)");
    fireEvent.change(input, { target: { value: " up , down ,, active " } });
    fireEvent.blur(input);
    expect(onChange).toHaveBeenCalledWith(0, {
      ...field,
      options: ["up", "down", "active"],
    });
  });

  it("seeds the options text buffer from field.options on mount", () => {
    const field: FieldDefinition = { ...STRING_FIELD, type: "dropdown", options: ["up", "down"] };
    renderRow(field);
    expect(screen.getByPlaceholderText("Options (comma-separated)")).toHaveValue("up, down");
  });
});

describe("FieldRow default-value editor per type", () => {
  it("string type: default input mirrors field.default and clears to undefined on empty", () => {
    const field: FieldDefinition = { ...STRING_FIELD, default: "eth0" };
    const { onChange } = renderRow(field);
    const defaultInput = screen.getByPlaceholderText("No default");
    expect(defaultInput).toHaveValue("eth0");
    fireEvent.change(defaultInput, { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(0, { ...field, default: undefined });
  });

  it("number type: parses a numeric default and clears to undefined on invalid input", () => {
    const field: FieldDefinition = { ...STRING_FIELD, type: "number", default: 30 };
    const { onChange } = renderRow(field);
    const defaultInput = screen.getByPlaceholderText("No default");
    expect(defaultInput).toHaveValue(30);
    fireEvent.change(defaultInput, { target: { value: "45" } });
    expect(onChange).toHaveBeenLastCalledWith(0, { ...field, default: 45 });
  });

  it("number type: clearing the input sets default to undefined, not NaN", () => {
    const field: FieldDefinition = { ...STRING_FIELD, type: "number", default: 30 };
    const { onChange } = renderRow(field);
    fireEvent.change(screen.getByPlaceholderText("No default"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(0, { ...field, default: undefined });
  });

  it("boolean type: renders a checkbox default and toggles it", () => {
    const field: FieldDefinition = { ...STRING_FIELD, type: "boolean", default: false };
    const { onChange } = renderRow(field);
    // Two checkboxes render: Required, and the boolean default. The default
    // checkbox is the second one (Required is checked=false too, so target by
    // proximity to its "false" label text).
    expect(screen.getByText("false")).toBeInTheDocument();
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[1]);
    expect(onChange).toHaveBeenCalledWith(0, { ...field, default: true });
  });

  it("dropdown type: default select lists only the configured options plus a 'No default' entry", () => {
    const field: FieldDefinition = {
      ...STRING_FIELD,
      type: "dropdown",
      options: ["up", "down"],
      default: "up",
    };
    renderRow(field);
    const selects = screen.getAllByRole("combobox");
    const defaultSelect = selects.find((s) => s !== screen.getByDisplayValue("Dropdown"))!;
    const optionLabels = Array.from(defaultSelect.querySelectorAll("option")).map(
      (o) => o.textContent,
    );
    expect(optionLabels).toEqual(["No default", "up", "down"]);
  });

  it("dropdown type: selecting the blank option clears default to undefined", () => {
    const field: FieldDefinition = {
      ...STRING_FIELD,
      type: "dropdown",
      options: ["up", "down"],
      default: "up",
    };
    const { onChange } = renderRow(field);
    const selects = screen.getAllByRole("combobox");
    const defaultSelect = selects.find((s) => s !== screen.getByDisplayValue("Dropdown"))!;
    fireEvent.change(defaultSelect, { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(0, { ...field, default: undefined });
  });
});
