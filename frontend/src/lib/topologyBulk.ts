import { isAdminRole } from "@/lib/roles";
import { sharedReason } from "@/lib/reservationBulk";
import type { BulkOutcome } from "@/lib/reservationBulk";
import type { Topology } from "@/types/topology.types";

/**
 * Pure logic behind the Topologies page's delete gate and multi-select Delete
 * (issue #958). The settled-result folding (`applySettled`, `sharedReason`) is
 * imported from reservationBulk.ts as is; only the topology-specific gate and
 * wording live here.
 */

type Caller = { id: string; role?: string | null } | null | undefined;

/**
 * Who may delete a topology: its creator, or role admin or superadmin. This is
 * exactly cabling's `delete_topology` rule (403 otherwise), never stricter and
 * never looser, and it is the ONE gate behind both the row's Delete button and
 * the bulk action.
 */
export function canDeleteTopologyAs(
  topology: Pick<Topology, "created_by">,
  user: Caller,
): boolean {
  if (!user) return false;
  return user.id === topology.created_by || isAdminRole(user.role);
}

export interface TopologyPartition {
  eligible: Topology[];
  /** Every skipped row is skipped for one reason: the caller may not delete it. */
  notYours: Topology[];
}

/** Split a selection per row, never per batch, through canDeleteTopologyAs. */
export function partitionTopologies(
  selected: readonly Topology[],
  user: Caller,
): TopologyPartition {
  const eligible: Topology[] = [];
  const notYours: Topology[] = [];
  for (const topology of selected) {
    if (canDeleteTopologyAs(topology, user)) eligible.push(topology);
    else notYours.push(topology);
  }
  return { eligible, notYours };
}

function plural(n: number): string {
  return `${n} topolog${n === 1 ? "y" : "ies"}`;
}

function consequence(n: number): string {
  return n === 1
    ? "This permanently deletes it and its canvas data and cannot be undone."
    : "This permanently deletes them and their canvas data and cannot be undone.";
}

export const BULK_DELETE_TITLE = "Delete Topologies";
export const BULK_DELETE_KEEP_LABEL = "Keep topologies";

export function bulkDeleteConfirmLabel(partition: TopologyPartition): string {
  return `Delete ${plural(partition.eligible.length)}`;
}

/**
 * How many will be deleted, how many skipped and why. Never called with
 * nothing eligible (the action is disabled then).
 */
export function bulkDeleteDescription(partition: TopologyPartition): string {
  const { eligible, notYours } = partition;
  const effect = consequence(eligible.length);
  if (notYours.length === 0) return `Delete ${plural(eligible.length)}? ${effect}`;
  const total = eligible.length + notYours.length;
  return (
    `Delete ${eligible.length} of the ${total} selected topologies? ${effect} ` +
    `${notYours.length} will be skipped: not yours.`
  );
}

/** Why Delete selected is disabled when no selected row is eligible. */
export function noneDeletableMessage(partition: TopologyPartition): string {
  return `None of the selected topologies can be deleted: ${partition.notYours.length} not yours.`;
}

/** The toast sentence: "Deleted 3 topologies" or "Deleted 2, failed 1: reason". */
export function summarizeDelete(outcome: BulkOutcome): string {
  if (outcome.failures.length === 0) return `Deleted ${plural(outcome.succeeded)}`;
  const base = `Deleted ${outcome.succeeded}, failed ${outcome.failures.length}`;
  const reason = sharedReason(outcome.failures);
  return reason ? `${base}: ${reason}` : base;
}
