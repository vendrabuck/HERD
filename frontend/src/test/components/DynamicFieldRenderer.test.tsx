import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DynamicFieldRenderer } from "@/components/devices/DynamicFieldRenderer";
import type { SectionDefinition } from "@/types/template.types";

const sections: SectionDefinition[] = [
  {
    name: "General",
    fields: [
      { key: "hostname", label: "Hostname", type: "string", required: true },
      { key: "port_count", label: "Port Count", type: "number" },
      { key: "enabled", label: "Enabled", type: "boolean" },
      {
        key: "vendor",
        label: "Vendor",
        type: "dropdown",
        options: ["Cisco", "Juniper", "Arista"],
      },
      {
        key: "location",
        label: "Location",
        type: "string",
        default: "Rack A",
      },
    ],
  },
];

describe("DynamicFieldRenderer", () => {
  it("renders text input for string fields", () => {
    render(
      <DynamicFieldRenderer sections={sections} fieldData={{}} onChange={() => {}} />,
    );
    const input = screen.getByLabelText(/Hostname/);
    expect(input).toHaveAttribute("type", "text");
  });

  it("renders number input for number fields", () => {
    render(
      <DynamicFieldRenderer sections={sections} fieldData={{}} onChange={() => {}} />,
    );
    const input = screen.getByLabelText(/Port Count/);
    expect(input).toHaveAttribute("type", "number");
  });

  it("renders checkbox for boolean fields", () => {
    render(
      <DynamicFieldRenderer sections={sections} fieldData={{}} onChange={() => {}} />,
    );
    const input = screen.getByLabelText(/Enabled/);
    expect(input).toHaveAttribute("type", "checkbox");
  });

  it("renders select with correct options for dropdown fields", () => {
    render(
      <DynamicFieldRenderer sections={sections} fieldData={{}} onChange={() => {}} />,
    );
    const select = screen.getByLabelText(/Vendor/);
    expect(select.tagName).toBe("SELECT");
    const options = select.querySelectorAll("option");
    // "Select..." placeholder + 3 vendor options
    expect(options).toHaveLength(4);
    expect(options[1].textContent).toBe("Cisco");
    expect(options[2].textContent).toBe("Juniper");
    expect(options[3].textContent).toBe("Arista");
  });

  it("marks required fields with asterisk", () => {
    render(
      <DynamicFieldRenderer sections={sections} fieldData={{}} onChange={() => {}} />,
    );
    const label = screen.getByText(/Hostname/);
    const asterisk = label.querySelector("span");
    expect(asterisk).toHaveTextContent("*");
  });

  it("calls onChange with updated fieldData on string input change", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <DynamicFieldRenderer
        sections={sections}
        fieldData={{ hostname: "" }}
        onChange={onChange}
      />,
    );
    const input = screen.getByLabelText(/Hostname/);
    await user.type(input, "A");
    expect(onChange).toHaveBeenCalledWith({ hostname: "A" });
  });

  it("calls onChange with boolean value on checkbox change", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <DynamicFieldRenderer
        sections={sections}
        fieldData={{ enabled: false }}
        onChange={onChange}
      />,
    );
    const checkbox = screen.getByLabelText(/Enabled/);
    await user.click(checkbox);
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ enabled: true }));
  });

  it("calls onChange with selected option on dropdown change", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <DynamicFieldRenderer
        sections={sections}
        fieldData={{}}
        onChange={onChange}
      />,
    );
    const select = screen.getByLabelText(/Vendor/);
    await user.selectOptions(select, "Juniper");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ vendor: "Juniper" }));
  });

  it("uses field default value when fieldData key is absent", () => {
    render(
      <DynamicFieldRenderer sections={sections} fieldData={{}} onChange={() => {}} />,
    );
    const input = screen.getByLabelText(/Location/) as HTMLInputElement;
    expect(input.value).toBe("Rack A");
  });
});
