import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

// Mock HTMLDialogElement methods (jsdom has no native <dialog> support)
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

// Mock react-router-dom, capturing navigate calls for the redirect-guard test.
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

// Mock react-hot-toast. The page uses both toast.error/toast.success AND the
// bare callable form (`toast(message)`) for the informational 409-busy case,
// so the mock default export must be callable as well as carry .error/.success.
const mockToast = vi.fn();
const mockToastError = vi.fn();
const mockToastSuccess = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: Object.assign(
    (...args: unknown[]) => mockToast(...args),
    {
      error: (...args: unknown[]) => mockToastError(...args),
      success: (...args: unknown[]) => mockToastSuccess(...args),
    },
  ),
}));

const ADMIN_USER = { id: "1", email: "admin@test.com", role: "admin", username: "admin" };
const REGULAR_USER = { id: "2", email: "user@test.com", role: "user", username: "regular" };
let currentUser: typeof ADMIN_USER | typeof REGULAR_USER | null = ADMIN_USER;
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: typeof ADMIN_USER | null }) => unknown) =>
    selector({ user: currentUser }),
}));

// Mock the ldap-sync api hooks wholesale, but keep the real pinned 409
// detail strings and the real isRunStale/STALE_RUNNING_THRESHOLD_MS so the
// page's 409-discrimination and staleness logic is exercised against the
// actual constants, not a copy that could silently drift from them.
const mockUseLdapSyncStatus = vi.fn();
const mockUsePaginatedMappings = vi.fn();
const mockCreateMapping = { mutateAsync: vi.fn(), isPending: false };
const mockDeleteMapping = { mutateAsync: vi.fn(), isPending: false };
const mockUsePaginatedSyncRuns = vi.fn();
const mockStartSyncRun = { mutateAsync: vi.fn(), isPending: false };

vi.mock("@/api/ldapSync", async () => {
  const actual = await vi.importActual<typeof import("@/api/ldapSync")>("@/api/ldapSync");
  return {
    RUN_IN_PROGRESS_DETAIL: actual.RUN_IN_PROGRESS_DETAIL,
    RUN_IN_PROGRESS_REPLICA_DETAIL: actual.RUN_IN_PROGRESS_REPLICA_DETAIL,
    STALE_RUNNING_THRESHOLD_MS: actual.STALE_RUNNING_THRESHOLD_MS,
    isRunStale: actual.isRunStale,
    useLdapSyncStatus: () => mockUseLdapSyncStatus(),
    usePaginatedMappings: (...args: unknown[]) => mockUsePaginatedMappings(...args),
    useCreateMapping: () => mockCreateMapping,
    useDeleteMapping: () => mockDeleteMapping,
    usePaginatedSyncRuns: (...args: unknown[]) => mockUsePaginatedSyncRuns(...args),
    useStartSyncRun: () => mockStartSyncRun,
  };
});

const mockUseGroups = vi.fn();
vi.mock("@/api/groups", () => ({
  useGroups: () => mockUseGroups(),
}));

import { LdapSyncPage } from "@/pages/admin/LdapSyncPage";
import { RUN_IN_PROGRESS_DETAIL, RUN_IN_PROGRESS_REPLICA_DETAIL, STALE_RUNNING_THRESHOLD_MS } from "@/api/ldapSync";

const SAMPLE_GROUPS = [
  { id: "g1", name: "Engineering", description: null, created_by: null, created_at: "2026-01-01T00:00:00Z" },
  { id: "g2", name: "QA", description: null, created_by: null, created_at: "2026-01-01T00:00:00Z" },
];

const SAMPLE_MAPPING = {
  id: "m1",
  group_dn: "cn=herd-eng,ou=groups,dc=company,dc=local",
  directory_name: "herd-eng",
  herd_group_id: "g1",
  created_by: null,
  created_at: "2026-01-01T00:00:00Z",
};

const SAMPLE_RUNS = [
  {
    id: "r-success",
    started_at: "2026-08-14T00:00:00Z",
    finished_at: "2026-08-14T00:00:05Z",
    trigger: "manual",
    status: "success",
    users_provisioned: 1,
    members_added: 2,
    members_removed: 0,
    members_skipped: 0,
    users_deactivated: 0,
    users_reactivated: 0,
    detail: {},
    error: null,
  },
  {
    id: "r-partial",
    started_at: "2026-08-14T01:00:00Z",
    finished_at: "2026-08-14T01:00:05Z",
    trigger: "interval",
    status: "partial",
    users_provisioned: 0,
    members_added: 0,
    members_removed: 0,
    members_skipped: 1,
    users_deactivated: 0,
    users_reactivated: 0,
    detail: { dangling: ["cn=gone,ou=groups,dc=company,dc=local"] },
    error: null,
  },
  {
    id: "r-aborted",
    started_at: "2026-08-14T02:00:00Z",
    finished_at: "2026-08-14T02:00:05Z",
    trigger: "interval",
    status: "aborted",
    users_provisioned: 0,
    members_added: 0,
    members_removed: 0,
    members_skipped: 0,
    users_deactivated: 0,
    users_reactivated: 0,
    detail: { reason: "breaker tripped" },
    error: null,
  },
  {
    id: "r-failed",
    started_at: "2026-08-14T03:00:00Z",
    finished_at: "2026-08-14T03:00:05Z",
    trigger: "manual",
    status: "failed",
    users_provisioned: 0,
    members_added: 0,
    members_removed: 0,
    members_skipped: 0,
    users_deactivated: 0,
    users_reactivated: 0,
    detail: {},
    error: "directory unreachable",
  },
  {
    id: "r-running",
    started_at: new Date().toISOString(),
    finished_at: null,
    trigger: "manual",
    status: "running",
    users_provisioned: 0,
    members_added: 0,
    members_removed: 0,
    members_skipped: 0,
    users_deactivated: 0,
    users_reactivated: 0,
    detail: {},
    error: null,
  },
];

/** Opens the create-mapping modal, fills both fields, and clicks Create. */
function fillAndSubmitCreateMapping(groupDn: string, herdGroupId: string) {
  fireEvent.click(screen.getByRole("button", { name: "Create mapping" }));
  fireEvent.change(screen.getByLabelText("Directory group DN"), { target: { value: groupDn } });
  fireEvent.change(screen.getByLabelText("HERD group"), { target: { value: herdGroupId } });
  const dialog = document.querySelectorAll("dialog")[0];
  const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
    (b) => b.textContent === "Create",
  )!;
  fireEvent.click(submitBtn);
}

describe("LdapSyncPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentUser = ADMIN_USER;
    mockUseLdapSyncStatus.mockReturnValue({
      data: { auth_method: "ldap", group_sync_enabled: true, sync_interval_seconds: 3600 },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockUsePaginatedMappings.mockReturnValue({
      data: { items: [SAMPLE_MAPPING], total: 1 },
      isLoading: false,
    });
    mockUsePaginatedSyncRuns.mockReturnValue({
      data: { items: SAMPLE_RUNS, total: SAMPLE_RUNS.length },
      isLoading: false,
    });
    mockUseGroups.mockReturnValue({ data: SAMPLE_GROUPS });
  });

  it("redirects a non-admin user and renders nothing", () => {
    currentUser = REGULAR_USER;
    render(<LdapSyncPage />);
    expect(mockNavigate).toHaveBeenCalledWith("/topology");
    expect(screen.queryByText("LDAP Directory Sync")).not.toBeInTheDocument();
  });

  // --- Item 1: status fetch error ---

  it("status fetch error renders a retry banner, no local-mode banner, and keeps actions disabled", () => {
    const mockRefetch = vi.fn();
    mockUseLdapSyncStatus.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch: mockRefetch,
    });
    render(<LdapSyncPage />);
    expect(screen.getByText("Could not load directory sync status.")).toBeInTheDocument();
    expect(
      screen.queryByText(/This deployment uses local authentication/),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create mapping" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sync now" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(mockRefetch).toHaveBeenCalled();
  });

  it("local-auth mode shows the inactive banner and disables create/sync-now", () => {
    mockUseLdapSyncStatus.mockReturnValue({
      data: { auth_method: "local", group_sync_enabled: false, sync_interval_seconds: 3600 },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<LdapSyncPage />);
    expect(
      screen.getByText(/This deployment uses local authentication; directory sync is inactive/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create mapping" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sync now" })).toBeDisabled();
    // Existing mappings still render for cleanup, per the ADR's any-mode rule.
    expect(screen.getByText(SAMPLE_MAPPING.group_dn)).toBeInTheDocument();
  });

  it("ldap mode with the loop enabled shows the interval and enables actions", () => {
    render(<LdapSyncPage />);
    expect(screen.getByText(/Automatic sync runs every 1 hour/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create mapping" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Sync now" })).not.toBeDisabled();
  });

  it("ldap mode with the loop disabled notes manual-only sync", () => {
    mockUseLdapSyncStatus.mockReturnValue({
      data: { auth_method: "ldap", group_sync_enabled: false, sync_interval_seconds: 3600 },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<LdapSyncPage />);
    expect(screen.getByText(/Automatic sync is disabled; runs are manual-only/)).toBeInTheDocument();
  });

  // --- Item 5: warning banner ---

  it("create mapping with a warning renders a persistent amber banner naming the group DN", async () => {
    mockCreateMapping.mutateAsync.mockResolvedValueOnce({
      ...SAMPLE_MAPPING,
      id: "m2",
      group_dn: "cn=empty,ou=groups,dc=company,dc=local",
      warning: "The directory entry reports no members.",
    });
    render(<LdapSyncPage />);
    fillAndSubmitCreateMapping("cn=empty,ou=groups,dc=company,dc=local", "g1");

    await waitFor(() =>
      expect(screen.getByText(/The directory entry reports no members\./)).toBeInTheDocument(),
    );
    expect(screen.getByText("cn=empty,ou=groups,dc=company,dc=local")).toBeInTheDocument();

    // Dismissible.
    fireEvent.click(screen.getByLabelText("Dismiss warning"));
    expect(
      screen.queryByText(/The directory entry reports no members\./),
    ).not.toBeInTheDocument();
  });

  it("a later successful create clears an earlier warning banner", async () => {
    mockCreateMapping.mutateAsync
      .mockResolvedValueOnce({
        ...SAMPLE_MAPPING,
        id: "m2",
        group_dn: "cn=empty,ou=groups,dc=company,dc=local",
        warning: "The directory entry reports no members.",
      })
      .mockResolvedValueOnce({
        ...SAMPLE_MAPPING,
        id: "m3",
        group_dn: "cn=herd-qa,ou=groups,dc=company,dc=local",
        warning: null,
      });
    render(<LdapSyncPage />);

    fillAndSubmitCreateMapping("cn=empty,ou=groups,dc=company,dc=local", "g1");
    await waitFor(() =>
      expect(screen.getByText(/The directory entry reports no members\./)).toBeInTheDocument(),
    );

    fillAndSubmitCreateMapping("cn=herd-qa,ou=groups,dc=company,dc=local", "g2");
    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalledWith("Mapping created"));
    expect(
      screen.queryByText(/The directory entry reports no members\./),
    ).not.toBeInTheDocument();
  });

  it("deleting the mapping a warning banner refers to clears the banner", async () => {
    mockCreateMapping.mutateAsync.mockResolvedValueOnce({
      ...SAMPLE_MAPPING,
      warning: "The directory entry reports no members.",
    });
    render(<LdapSyncPage />);
    fillAndSubmitCreateMapping(SAMPLE_MAPPING.group_dn, "g1");
    await waitFor(() =>
      expect(screen.getByText(/The directory entry reports no members\./)).toBeInTheDocument(),
    );

    fireEvent.click(screen.getAllByText("Delete")[0]);
    const confirmDialog = document.querySelectorAll("dialog")[1];
    const confirmBtn = Array.from(confirmDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Delete",
    )!;
    fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(
        screen.queryByText(/The directory entry reports no members\./),
      ).not.toBeInTheDocument(),
    );
  });

  it("create mapping without a warning shows a plain success toast, no banner", async () => {
    mockCreateMapping.mutateAsync.mockResolvedValueOnce({ ...SAMPLE_MAPPING, warning: null });
    render(<LdapSyncPage />);
    fillAndSubmitCreateMapping(SAMPLE_MAPPING.group_dn, "g1");
    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalledWith("Mapping created"));
  });

  it("create mapping surfaces a 409/422/503 detail via toast.error", async () => {
    mockCreateMapping.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "A mapping for this group_dn already exists" } },
    });
    render(<LdapSyncPage />);
    fillAndSubmitCreateMapping(SAMPLE_MAPPING.group_dn, "g1");

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("A mapping for this group_dn already exists"),
    );
  });

  it("delete shows a confirm dialog and, on confirm, deletes and toasts", async () => {
    render(<LdapSyncPage />);
    fireEvent.click(screen.getAllByText("Delete")[0]);
    expect(screen.getByText("Delete mapping")).toBeInTheDocument();
    expect(
      screen.getByText(/keeps its current members; it simply stops syncing from the directory/),
    ).toBeInTheDocument();

    const confirmDialog = document.querySelectorAll("dialog")[1];
    const confirmBtn = Array.from(confirmDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Delete",
    )!;
    fireEvent.click(confirmBtn);

    await waitFor(() => expect(mockDeleteMapping.mutateAsync).toHaveBeenCalledWith("m1"));
    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalledWith("Mapping deleted"));
  });

  // --- Item 3: pagination clamp ---

  it("clamps mappings skip to the last valid page after a delete empties the current page", async () => {
    // 51 rows total; page 2 (skip=50) holds exactly the one row being deleted.
    mockUsePaginatedMappings.mockReturnValue({
      data: { items: [{ ...SAMPLE_MAPPING, id: "m-last" }], total: 51 },
      isLoading: false,
    });
    render(<LdapSyncPage />);

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(mockUsePaginatedMappings).toHaveBeenLastCalledWith(50, 50));

    mockDeleteMapping.mutateAsync.mockImplementation(async () => {
      // Simulate the post-invalidation refetch: page 2 is now empty.
      mockUsePaginatedMappings.mockReturnValue({
        data: { items: [], total: 50 },
        isLoading: false,
      });
    });

    fireEvent.click(screen.getAllByText("Delete")[0]);
    const confirmDialog = document.querySelectorAll("dialog")[1];
    const confirmBtn = Array.from(confirmDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Delete",
    )!;
    fireEvent.click(confirmBtn);

    await waitFor(() => expect(mockUsePaginatedMappings).toHaveBeenLastCalledWith(0, 50));
  });

  // --- Item 2: sync-now 409 discrimination ---

  it("sync now starts a run, resets to page 1, and toasts success on 202", async () => {
    mockUsePaginatedSyncRuns.mockReturnValue({
      data: { items: SAMPLE_RUNS, total: 60 },
      isLoading: false,
    });
    mockStartSyncRun.mutateAsync.mockResolvedValueOnce({ run_id: "r-new" });
    render(<LdapSyncPage />);

    fireEvent.click(screen.getAllByRole("button", { name: "Next" })[0]);
    await waitFor(() => expect(mockUsePaginatedSyncRuns).toHaveBeenLastCalledWith(50, 50));

    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalledWith("Sync run started"));
    await waitFor(() => expect(mockUsePaginatedSyncRuns).toHaveBeenLastCalledWith(0, 50));
  });

  it("sync now shows an informational (non-error) toast on the in-process 409 lock detail", async () => {
    mockStartSyncRun.mutateAsync.mockRejectedValueOnce({
      response: { status: 409, data: { detail: RUN_IN_PROGRESS_DETAIL } },
    });
    render(<LdapSyncPage />);
    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith(RUN_IN_PROGRESS_DETAIL));
    expect(mockToastError).not.toHaveBeenCalled();
  });

  it("sync now shows an informational toast on the cross-replica 409 lock detail", async () => {
    mockStartSyncRun.mutateAsync.mockRejectedValueOnce({
      response: { status: 409, data: { detail: RUN_IN_PROGRESS_REPLICA_DETAIL } },
    });
    render(<LdapSyncPage />);
    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith(RUN_IN_PROGRESS_REPLICA_DETAIL));
    expect(mockToastError).not.toHaveBeenCalled();
  });

  it("sync now surfaces the mode-refusal 409 as an error, not informational", async () => {
    mockStartSyncRun.mutateAsync.mockRejectedValueOnce({
      response: { status: 409, data: { detail: "Directory sync requires auth_method=ldap" } },
    });
    render(<LdapSyncPage />);
    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Directory sync requires auth_method=ldap"),
    );
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("sync now surfaces a non-409 failure via toast.error", async () => {
    mockStartSyncRun.mutateAsync.mockRejectedValueOnce({
      response: { status: 500, data: { detail: "boom" } },
    });
    render(<LdapSyncPage />);
    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(mockToastError).toHaveBeenCalledWith("boom"));
  });

  // --- Runs table ---

  it("renders the runs table with a semantic badge per status", () => {
    render(<LdapSyncPage />);
    expect(screen.getByText("success")).toBeInTheDocument();
    expect(screen.getByText("partial")).toBeInTheDocument();
    expect(screen.getByText("aborted")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("directory unreachable")).toBeInTheDocument();
  });

  // --- Item 4b: staleness cap ---

  it("renders a stale running row as 'running (stale)' with an explanatory title", () => {
    const staleStartedAt = new Date(Date.now() - STALE_RUNNING_THRESHOLD_MS - 60_000).toISOString();
    mockUsePaginatedSyncRuns.mockReturnValue({
      data: {
        items: [{ ...SAMPLE_RUNS[4], id: "r-stale-running", started_at: staleStartedAt }],
        total: 1,
      },
      isLoading: false,
    });
    render(<LdapSyncPage />);
    const badge = screen.getByText("running (stale)");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute(
      "title",
      expect.stringContaining("may belong to a crashed process"),
    );
  });

  it("a running row within the staleness window renders the plain 'running' badge", () => {
    const freshStartedAt = new Date(Date.now() - 60_000).toISOString();
    mockUsePaginatedSyncRuns.mockReturnValue({
      data: {
        items: [{ ...SAMPLE_RUNS[4], id: "r-fresh-running", started_at: freshStartedAt }],
        total: 1,
      },
      isLoading: false,
    });
    render(<LdapSyncPage />);
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.queryByText("running (stale)")).not.toBeInTheDocument();
  });

  it("expands a run's detail JSON behind a Details toggle", () => {
    render(<LdapSyncPage />);
    const detailButtons = screen.getAllByText("Details");
    fireEvent.click(detailButtons[0]);
    expect(screen.getByText(/dangling/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Hide"));
    expect(screen.queryByText(/dangling/)).not.toBeInTheDocument();
  });
});
