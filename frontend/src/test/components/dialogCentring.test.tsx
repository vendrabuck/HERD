// Issue #979: every modal <dialog> must be centred in the viewport.
//
// Tailwind 4's preflight sets `margin: 0` on every element, which overrides
// the user-agent `margin: auto` that centres a modal dialog, so a dialog with
// no margin class renders pinned to the top-left corner. The fix is an
// explicit `m-auto` on each dialog. jsdom has no layout, so these tests pin
// the class itself; the live geometry is asserted by
// tests/e2e/test_dialog_centring_playwright.py (nightly).
//
// Height stays bounded by the user-agent `:modal` rule
// (`max-height: calc(100% - 6px - 2em); overflow: auto`), which preflight does
// not reset, so a centred dialog taller than the viewport scrolls inside
// itself instead of overflowing both edges.
import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Modal } from "@/components/ui/Modal";
import { BulkImportExport } from "@/components/ui/BulkImportExport";

const CENTRING_CLASS = "m-auto";

// The known native dialogs. A new one must be added here AND carry the class.
const KNOWN_DIALOG_FILES = [
  "/src/components/ui/BulkImportExport.tsx",
  "/src/components/ui/ConfirmDialog.tsx",
  "/src/components/ui/Modal.tsx",
];

function dialogOf(container: HTMLElement): HTMLDialogElement {
  const dialog = container.querySelector("dialog");
  if (!dialog) throw new Error("no <dialog> rendered");
  return dialog;
}

describe("dialog centring (issue #979)", () => {
  it("ConfirmDialog carries the centring class", () => {
    const { container } = render(
      <ConfirmDialog
        open={false}
        title="Delete?"
        description="Gone for good."
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(dialogOf(container)).toHaveClass(CENTRING_CLASS);
  });

  it("Modal carries the centring class with the default width", () => {
    const { container } = render(
      <Modal open={false} onClose={vi.fn()} title="New Topology">
        body
      </Modal>,
    );
    expect(dialogOf(container)).toHaveClass(CENTRING_CLASS);
    expect(dialogOf(container)).toHaveClass("max-w-lg");
  });

  it("Modal keeps the centring class when a caller overrides the width", () => {
    const { container } = render(
      <Modal open={false} onClose={vi.fn()} title="Wire" className="max-w-[824px]">
        body
      </Modal>,
    );
    const dialog = dialogOf(container);
    expect(dialog).toHaveClass(CENTRING_CLASS);
    expect(dialog).toHaveClass("max-w-[824px]");
    expect(dialog).not.toHaveClass("max-w-lg");
  });

  it("BulkImportExport's import dialog carries the centring class", () => {
    const { container } = render(
      <BulkImportExport resourceLabel="devices" onExport={vi.fn()} onImport={vi.fn()} />,
    );
    expect(dialogOf(container)).toHaveClass(CENTRING_CLASS);
  });
});

// Structural scan: every native <dialog> anywhere under src (tests excluded)
// must carry the centring class, so a fourth dialog cannot regress silently.
const sources = import.meta.glob<string>(["/src/**/*.tsx", "!/src/test/**"], {
  query: "?raw",
  import: "default",
  eager: true,
});

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const DIALOG_OPEN = /<dialog(?=[\s>])/g;
const DIALOG_CLASS = /<dialog\b[^>]*?className=(?:"([^"]*)"|\{`([^`]*)`\})/g;

describe("dialog centring structural scan (issue #979)", () => {
  const withDialogs = Object.entries(sources)
    .map(([path, raw]) => [path, stripComments(raw)] as const)
    .filter(([, code]) => (code.match(DIALOG_OPEN) ?? []).length > 0);

  it("scans a non-empty source tree", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it("finds exactly the known dialog components", () => {
    expect(withDialogs.map(([path]) => path).sort()).toEqual(KNOWN_DIALOG_FILES);
  });

  it.each(withDialogs)("every <dialog> in %s has a static className with m-auto", (_path, code) => {
    const opens = code.match(DIALOG_OPEN) ?? [];
    const classNames = Array.from(code.matchAll(DIALOG_CLASS), (m) => m[1] ?? m[2]);
    // Each opening tag must expose its className for this check to see.
    expect(classNames).toHaveLength(opens.length);
    for (const className of classNames) {
      expect(className.split(/\s+/)).toContain(CENTRING_CLASS);
    }
  });

  it("fails a dialog without the class (scanner self-check)", () => {
    const bad = '<dialog ref={r} className="rounded-lg max-w-sm w-full">';
    const classNames = Array.from(bad.matchAll(DIALOG_CLASS), (m) => m[1] ?? m[2]);
    expect(classNames).toEqual(["rounded-lg max-w-sm w-full"]);
    expect(classNames[0].split(/\s+/)).not.toContain(CENTRING_CLASS);
  });
});
