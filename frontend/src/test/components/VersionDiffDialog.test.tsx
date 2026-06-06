import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

import type {
  TopologyDiff,
  TopologyVersion,
} from "@/types/topology.types";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

const mockUseVersionDiff = vi.fn();

vi.mock("@/api/topologies", () => ({
  useVersionDiff: (...args: unknown[]) => mockUseVersionDiff(...args),
}));

import { VersionDiffDialog } from "@/components/topology-editor/VersionDiffDialog";

function makeVersion(n: number): TopologyVersion {
  return {
    id: `ver-${n}`,
    topology_id: "topo-1",
    version_number: n,
    name: `v${n}`,
    description: null,
    created_by: "user-1",
    author_name: "admin",
    created_at: "2026-05-31T00:00:00Z",
    restored_from_id: null,
  };
}

function emptyDiff(): TopologyDiff {
  return {
    version_a: "ver-1",
    version_b: "ver-2",
    nodes_added: [],
    nodes_removed: [],
    nodes_modified: [],
    edges_added: [],
    edges_removed: [],
    edges_modified: [],
  };
}

function baseProps() {
  return {
    open: true,
    topologyId: "topo-1",
    versionA: makeVersion(1),
    versionB: makeVersion(2),
    onClose: vi.fn(),
  };
}

describe("VersionDiffDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the version-range title and forwards version ids to the diff hook", () => {
    mockUseVersionDiff.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });

    render(<VersionDiffDialog {...baseProps()} />);

    expect(screen.getByText("Diff v1 to v2")).toBeInTheDocument();
    expect(mockUseVersionDiff).toHaveBeenCalledWith("topo-1", "ver-1", "ver-2");
  });

  it("shows the loading message while the diff is computing", () => {
    mockUseVersionDiff.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });

    render(<VersionDiffDialog {...baseProps()} />);

    expect(screen.getByText("Computing diff...")).toBeInTheDocument();
  });

  it("shows the error message when the diff request fails", () => {
    mockUseVersionDiff.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });

    render(<VersionDiffDialog {...baseProps()} />);

    expect(screen.getByText("Failed to compute diff.")).toBeInTheDocument();
  });

  it("renders all six sections with zero counts when the diff is empty", () => {
    mockUseVersionDiff.mockReturnValue({
      data: emptyDiff(),
      isLoading: false,
      isError: false,
    });

    render(<VersionDiffDialog {...baseProps()} />);

    expect(screen.getByText("Nodes added")).toBeInTheDocument();
    expect(screen.getByText("Nodes removed")).toBeInTheDocument();
    expect(screen.getByText("Nodes modified")).toBeInTheDocument();
    expect(screen.getByText("Edges added")).toBeInTheDocument();
    expect(screen.getByText("Edges removed")).toBeInTheDocument();
    expect(screen.getByText("Edges modified")).toBeInTheDocument();
    // Every section reports a zero count for an empty diff.
    expect(screen.getAllByText("(0)")).toHaveLength(6);
  });

  it("lists added-node ids and reveals the payload when a row is expanded", () => {
    const diff = emptyDiff();
    diff.nodes_added = [{ id: "node-a", kind: "switch" }];

    mockUseVersionDiff.mockReturnValue({
      data: diff,
      isLoading: false,
      isError: false,
    });

    render(<VersionDiffDialog {...baseProps()} />);

    // The section auto-opens because its count is greater than zero.
    expect(screen.getByText("Nodes added")).toBeInTheDocument();
    expect(screen.getByText("(1)")).toBeInTheDocument();

    const row = screen.getByRole("button", { name: /node-a/, hidden: true });
    // Payload is hidden until the row is clicked.
    expect(screen.queryByText(/"kind": "switch"/)).not.toBeInTheDocument();

    fireEvent.click(row);
    expect(screen.getByText(/"kind": "switch"/)).toBeInTheDocument();
  });

  it("renders before and after panes for a modified node when expanded", () => {
    const diff = emptyDiff();
    diff.nodes_modified = [
      {
        id: "node-m",
        before: { label: "old-name" },
        after: { label: "new-name" },
      },
    ];

    mockUseVersionDiff.mockReturnValue({
      data: diff,
      isLoading: false,
      isError: false,
    });

    render(<VersionDiffDialog {...baseProps()} />);

    const row = screen.getByRole("button", { name: /node-m/, hidden: true });
    fireEvent.click(row);

    expect(screen.getByText("before")).toBeInTheDocument();
    expect(screen.getByText("after")).toBeInTheDocument();
    expect(screen.getByText(/"label": "old-name"/)).toBeInTheDocument();
    expect(screen.getByText(/"label": "new-name"/)).toBeInTheDocument();
  });

  it("falls back to the generic title when a version is missing", () => {
    mockUseVersionDiff.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
    });

    render(
      <VersionDiffDialog {...baseProps()} versionA={null} versionB={null} />,
    );

    expect(screen.getByText("Version diff")).toBeInTheDocument();
    expect(mockUseVersionDiff).toHaveBeenCalledWith(
      "topo-1",
      undefined,
      undefined,
    );
  });
});
