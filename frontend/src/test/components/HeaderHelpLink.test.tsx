import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HeaderHelpLink } from "@/components/layout/HeaderHelpLink";

describe("HeaderHelpLink", () => {
  it("is a link named Help that points at the published manual", () => {
    render(<HeaderHelpLink />);
    const link = screen.getByRole("link", { name: "Help" });
    // The literal on purpose (issue #960): the published manual, not the
    // markdown sources on GitHub.
    expect(link).toHaveAttribute("href", "https://vendrabuck.github.io/HERD/manual/");
  });

  it("opens in a new tab without an opener or referrer", () => {
    render(<HeaderHelpLink />);
    const link = screen.getByRole("link", { name: "Help" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("hides its icon from assistive technology", () => {
    render(<HeaderHelpLink />);
    const icon = screen.getByRole("link", { name: "Help" }).querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });
});
