# Decision: The Wiring Reconcile Respects the Drive Claim, Issue #1481

Status: Proposed 2026-11-20. The owner has decided the shape: option 2 of issue #1481
(the reconcile takes the #817 drive claim for the rows it writes or drives, and skips a
row a retry pass holds). Option 1 alone and option 3 are rejected (see Options
rejected). Decisions D1 to D10 below are this record's recommendations for the parts
the issue left open; each waits for the owner's read, and Open questions lists what
could not be settled from the code. Deciders: the owner. No code in this document.
Context verified by reading the HERD-public tree at `9a231984` on 2026-11-20; symbols
are the stable reference and line numbers are as of that commit. Nothing here was
executed: every trace below is a reading of the code, and the test plan names the
interleavings that have to prove it. This area has been fixed and reverted three times,
so a trace that is only read is marked as such.

## Context

### The claim, and who honours it today

A wiring ledger row (`l1_connection_assignments`, `l2_port_assignments`,
`route_assignments`) carries `claimed_until`. A retry pass takes the claim with one
compare-and-swap immediately before the row's own driver call
(`WiringRowClaims.claim` in `services/execution/app/services/wiring_claim.py`): the
update matches the row id, status `FAILED`, and a stamp that is null or in the past,
and writes now plus `claim_budget()` (5 minutes at the defaults). Issue #817 added it so
the retry tick and the manual retry never drive one row at once. Issue #1479 (PR #1486)
made every failure write and every settle a retry pass makes respect it
(`claim_write_clauses`, `write_guard`), judged by the stamp in the database
(WIRE-CLAIM-8, WIRE-CLAIM-9).

Three kinds of writer still ignore it; WIRE-CLAIM-8 and the docstring of
`claim_write_clauses` name them as limits:

- the `wiring_changed` reconcile (`handle_wiring_changed`, `_reconcile_l2_memberships`,
  `_reconcile_l3_adjacency` in `services/execution/app/services/nats_consumer.py`);
- the terminal teardown (`_teardown_from_ledgers`, same module), which issue #1481 lists
  among the reconcile's sites although it is a separate pass with a separate trigger;
- a retry holder's own success and release flips, which are unconditional by decision.

### Terms

- A **pass** is one run of: a `wiring_changed` apply, a terminal teardown, the retry
  tick, a manual retry, or the settle pass this record adds.
- A row is **held** at time t when `claimed_until` is later than t. Its **holder** is
  the pass whose claim wrote that exact stamp (`WiringRowClaims.held`). Only a `FAILED`
  row can be held: the claim requires `FAILED`, and every flip out of `FAILED` clears
  the stamp (WIRE-CLAIM-2).
- A row's **key** is (switch, canonical port pair) for L1, (switch, port) for L2, and
  (switch) for L3, always within one reservation.
- To **skip** a row is to make no park, no driver call, and no ledger write for its
  key in this pass.

### What the reconcile does to a FAILED row today

| Layer | Where a reconcile or teardown meets a FAILED row | Function (line at `9a231984`) | Checks the claim |
|---|---|---|---|
| L1 | stale-build park, full reconcile only | `handle_wiring_changed` calls `park_stale_l1_build` (`nats_consumer.py:5001`); the park clears the stamp (`l1_assignment_service.py:907`) | no |
| L1 | release drive of the parked pair; build drive over a FAILED row | `_apply_wiring_pairs` (`:2242`), `_apply_one_port_action` (`:2544`); the claim at `:2592` runs only when a ledger is passed, and the calls at `:5010` and `:5029` pass none | no |
| L1 | load failure, login failure, op failure, unresolvable hop | key branch of `record_l1_failed` (`l1_assignment_service.py:736`; stamp cleared at `:855`) | no |
| L2 | stale-build park; direct release of a stale row with a nil or unknown allocation | `_reconcile_l2_memberships` calls `park_stale_l2_build` (`:3825`) and `release_l2_membership` (`:3814`) | no |
| L2 | drives and failure writes | `_apply_l2_memberships` (`:3246`), `_apply_one_vlan_action` (`:3561`), key branch of `record_l2_failed` (`l2_membership_service.py:401`, `:496`) | no |
| L3 | stale-build park | `_reconcile_l3_adjacency` calls `park_stale_route_build` (`:4799`); the park clears the stamp (`route_service.py:773`) | no |
| L3 | drive-gate refusal write | `record_route_failed` by key (`:4761`) | no |
| L3 | release of an unapplied pin | `release_unapplied_route_pin` (`route_service.py:890`) | yes: its select carries `unclaimed`, and a held row is left alone |
| L3 | provision over a FAILED pin, deprovision, failure writes | `_apply_l3_adjacency` (`:4224`; claim at `:4478`), key branch of `record_route_failed` (`route_service.py:584`, `:680`) | no |
| Teardown | park of each FAILED intended-`ACTIVE` pin, release of a pin with no routes, deprovision | `_teardown_from_ledgers` (`:5063`; park `:5171`, release `:5175`, apply `:5179`) | no |

Four things in the code differ from the issue's description, and each one changes the
design:

1. The reconcile is not uniformly blind to the claim: `release_unapplied_route_pin`
   already leaves a held pin alone. That call has no way to say it skipped, so today
   the skip is silent.
2. The L1 stale park exists only on the full-reconcile path. On the carried-delta path
   (`fork_version` exactly one above the last applied, with a delta) a released pair
   whose row is `FAILED` intended `ACTIVE` is not parked at all: `pair_needs_release`
   answers false for it and `_apply_wiring_pairs` moves on. L2 and L3 always run the
   full reconcile (WIRE-ORDER-8).
3. The issue says that after #1479 the retry holder no longer undoes the reconcile's
   park. That holds for the holder's failure writes other than the L3 partial failure.
   Its success flip is unconditional: `record_l1_connect`,
   `record_l2_membership_active`, and `record_route_active` flip the parked row `ACTIVE`
   by a compare-and-swap on status alone, or insert a new `ACTIVE` row when the
   reconcile already released it. The L3 partial-failure write also lands
   (`holder_or_unclaimed`) and turns the parked pin back to intended `ACTIVE`.
   `test_wiring_claim.py`
   (`test_after_the_reconcile_clears_the_claim_the_holders_success_is_recorded`,
   `test_after_the_reconcile_clears_the_claim_an_l3_partial_failure_is_recorded`) pins
   both.
4. The claim does not check direction. A retry pass that loaded a row as a build, and
   whose row another pass then parked intended `RELEASED`, still wins the claim (the row
   is `FAILED` and unclaimed) and drives the build it planned. This is reachable today
   between two retry passes and is not in the issue. It is derived by reading, not
   reproduced.

### Facts about the consumer that bound "run again later"

- One durable pull consumer, `max_deliver` 5, no `backoff` (`start_nats_consumer`,
  `NATS_MAX_DELIVER`). A transient failure is `msg.nak(delay=...)` with the schedule
  `NATS_NAK_BACKOFF_SECONDS`, default `1,5,15,60,120` (`services/execution/app/config.py`,
  `nak_delay` in `services/common/herd_common/jetstream.py`). The fifth failing delivery
  is dead-lettered and acked (`process_reservation_message`). So a message has at most
  four redeliveries, spread over 1 + 5 + 15 + 60 = 81 seconds; the fifth entry is never
  reached. A claim lasts 300 seconds.
- A dead-lettered `wiring_changed` is not staged again. Reservations' sweep heals only
  when cabling's fork version is above its own `fork_wiring_ledger`
  (`_heal_wiring_staging` in `services/reservations/app/tasks/expiration.py`,
  RES-FORK-13), and that ledger advanced when the event was staged.
- The loop fetches one message per pull and handles it to completion
  (`_consumer_loop`, WIRE-CONSUME-2). A handler that waits blocks every reservation's
  events on that replica, terminal events included.
- A redelivered event whose version was stamped is a no-op
  (`handle_wiring_changed`, WIRE-ORDER-3), so a redelivery only helps if the version is
  left unstamped. Later messages of the same reservation are not held back behind a
  nacked one (JetStream's behaviour for a consumer config that sets no
  `max_ack_pending`; stated from the config, not exercised here); the version check and
  the gap rule (WIRE-ORDER-4) are what keep that safe.
- Execution has a transactional outbox (`services/execution/app/models/outbox.py`), used
  for `herd.health.status_changed`. `OutboxMixin` has no not-before column, and
  execution consumes only `herd.reservations.*`, a subject reservations owns
  (RES-EVENT-2: the event exists if and only if reservations' ledger advanced).
- The retry loop (`run_wiring_retry_loop` in
  `services/execution/app/services/wiring_retry_service.py`) already runs every
  `WIRING_RETRY_INTERVAL_SECONDS` (60) in every replica that enables it, off the
  consumer, on durable rows, with no delivery count.

### Three attempts that failed

| Attempt | What it did | What broke |
|---|---|---|
| #1479 round 1 | Added the claim condition to the retry failure writes by row id only | The guarantee was narrower than stated: a retry pass's settles on rows it never claimed, and a holder whose claim had expired and been retaken, still wrote (commit `f5a98551` records the finding) |
| #1479 round 2 | Put the holder's success and release flips behind the stamp too | A writer that never claims (the teardown park) cleared the stamp during a held L3 provision; the holder's success was refused and the configured routes stayed on the switch with no ledger row (commit `fd9ccbf2` records it) |
| #1479 round 3, `holder_record` (as of commit `a8b14242`) | The holder decided from the row as it found it: PARK an addition that landed on a row settled toward release, REOPEN a removal that landed on a row rebuilt `ACTIVE` | PARK wrote by key. When held row X had been released during the drive and the key rebuilt as a new row Y, PARK flipped Y to `FAILED` intended `RELEASED`, and the release channel would tear down a wire the fork intends (commit `b8e728b2` records it) |

`a8b14242` is in main's history: the #1486 branch was merged by a merge commit, and
`b8e728b2` removed `holder_record` later on the same branch. The design is read with
`git show a8b14242:<path>`, or as the reverse of `git show b8e728b2`.

Rounds 2 and 3 share one cause: each tried to make the holder cope with a row that
another writer had changed under it. This record removes the other writer instead.
Round 1's lesson is separate: a guarantee built writer by writer misses writers.

### The two invariants

Let live(X, t) mean row X's `claimed_until` is later than t, and holds(P, X, t) mean
live(X, t) and the stamp equals the one pass P's claim wrote.

- **EXCLUSION.** For every row X and time t: (E1) at most one pass has a driver call
  in flight for X; (E2) while live(X, t), a write to X is made only by the P with
  holds(P, X, t). One exception to E2 is kept from main on purpose: a pass that changed
  the device always records it (the unconditional success and release flips, and the L3
  partial-failure write). E1 makes that exception unreachable while each drive ends
  inside its claim.
- **CONSERVATION.** No row is left in a state no pass will ever settle. Call a row
  owed when its status and direction disagree with what its reservation's current
  intent and freeze require. Every owed row is covered by at least one of: (a) the
  tick's selects return it (WIRE-RETRY-13, WIRE-RETRY-14); (b) a wiring or terminal
  event for the reservation is unconsumed; (c) the reservation's settle mark (D5) is
  unpaid; (d) the row is held, so its holder will write it; or the row is terminal by an
  existing decision (a pinned reason, a row at the attempts cap, a failed L1 or L2 build
  on a frozen reservation). Every intended change is eventually applied or recorded
  `FAILED`.

On main, effects 1 to 3 of the issue break E1 and E2. The harder half is CONSERVATION:
once the reconcile skips, something must be owed (c), because a holder that succeeds
leaves an `ACTIVE` row, and no retry channel reads `ACTIVE` rows.

## Decision

### D1. Every pass that writes or drives wiring owns a claim ledger

The reconcile and the terminal teardown each construct one `WiringRowClaims` per pass
and hand it, with a map from key to the id of this reservation's `FAILED` row for that
key, to `_apply_wiring_pairs`, `_apply_l2_memberships`, and `_apply_l3_adjacency`. The
map is built from the per-reservation FAILED reads the reconcile already makes
(`failed_assignments_for_reservation`, `failed_memberships_for_reservation`,
`failed_route_assignments_for_reservation`); the carried-delta path gains the L1 read.

This reuses what #817 and #1479 built for the retry channels, unchanged: the applies
claim a mapped row immediately before its own driver call (an L3 pin once, before its
switch's login) and drop a row whose claim is lost with no run, no call, and no write
(WIRE-CLAIM-4, WIRE-CLAIM-5); a failure write for a mapped key goes by row id under
`claim_write_clauses`, so a load or login failure recorded before any claim is refused
on a row another pass holds. A key with no `FAILED` row (a fresh build, or the release
of an `ACTIVE` row) has nothing to claim and keeps today's path.

After the last phase no caller passes `claims=None`, and the branches that exist only
for it (`write_guard`'s early yield, the retry-only wording) are deleted. A guard test
pins that every call of a ledger writer and of the three applies passes a ledger.

### D2. Reads stay unfiltered; the gate is at the write and at the drive

The three per-reservation FAILED reads keep returning every FAILED row, held or not.
Two reasons, the second new:

- the reason `wiring_claim.py` records: the stale-build widening must see every FAILED
  row, or a row it is responsible for drops out of the release side silently;
- a held row hidden from the read would make its key look like a key with no row, and
  the reconcile would start a fresh build by key next to the holder's drive. Under D1
  the read is what maps the key to the row that must be claimed, so filtering it would
  break E1.

A predicate on a read is also a check-then-act race; a compare-and-swap at the write
and at the drive is the only point where held or not held is decided atomically.

### D3. What a pass does at each site, and what skip means

| Site | Rule | If the row is held by another pass |
|---|---|---|
| Stale-build park (`park_stale_l1_build`, `park_stale_l2_build`, `park_stale_route_build`), reconcile and teardown | Runs inside `write_guard` with the pass's ledger, as a retry pass's park already does (`_reattempt_rows`) | Refused: the row is not added to the release side, and it joins `claims.lost` |
| Release with no driver call (the L2 nil or unknown allocation release, `release_unapplied_route_pin`, teardown's pin with no routes) | Inside `write_guard` by row id | Refused; joins `lost`. `release_unapplied_route_pin` keeps its own check and gains the report |
| Drive over a FAILED row (a build over a failed row of either direction, the release of a parked row, an L3 provision over a failed pin) | Claim by row id immediately before the driver call | No run, no driver call, no write; joins `lost` |
| Failure write for a key that maps to a FAILED row (load, login, op, gate refusal, `create_vlan` failure) | By row id under `claim_write_clauses` | Refused; joins `lost` |
| Failure write for a key with no mapped row | Key branch, as today | See the backstop below |

The backstop answers how a writer that has no row id finds and respects a held row. The
key branches of `record_l1_failed`, `record_l2_failed`, and `record_route_failed` already
select this reservation's non-`RELEASED` row for the key. They gain one test on the row
they found: when it is `FAILED` and carries a live stamp that is not the calling pass's
own, they write nothing, add its id to `claims.lost`, and return None. The select takes
`FOR UPDATE`, so the test and the write are one critical section. The map of D1 is what
lets a pass claim before it drives; the backstop is what makes E2 true at the write site
whatever the caller knew, including a row that appeared after the pass's read.

The backstop is on failure writes and settles only. It is never put on a success or
release flip (see D6): a refused failure write loses an attempt count and an error
text, while a refused success loses the record of a device change, which is round 2's
defect.

A park never ends in a drive by a pass that does not hold the row: the parking pass
claims the parked row before it drives the removal, like any other release-direction
row.

### D4. A claim is a compare-and-swap on what the pass read

`WiringRowClaims.claim` gains one predicate: the row's `intended` still equals the value
the pass read when it planned the drive. A pass whose row another pass parked in the
meantime loses the claim and drives nothing.

This is what makes a successful park final against every stale build plan. A park
(under `write_guard`) and a claim are both conditional writes on one row, so Postgres
orders them:

- the claim commits first: the guard's select does not match and the park is refused
  (the row is held), so the parking pass skips and owes a settle (D5);
- the park commits first: the row is intended `RELEASED`, and a pass that read it as a
  build can no longer claim it. Only a pass that read the parked state (the parking pass
  itself, or a release-direction retry) can claim it, and that pass drives the removal.

There is no third order. Without D4 the second case leaves a hole: the parking pass's
removal fails, the row sits `FAILED` intended `RELEASED` and unclaimed, a stale retry
pass claims it and connects, and the row goes `ACTIVE` for a wire nobody intends with
nothing owed.

A claim lost this way is not another pass's work in flight. The pass's read-back reports
such a row by its status and claim: still `FAILED` and not held reads `still_failed`,
not `in_progress`.

### D5. Who settles a skipped row: a durable settle mark, paid by a settle pass

The reconcile does not wait and does not ask for a redelivery. It finishes everything
else, records that it owes a settle, stamps the version, and acks.

**The mark.** `reservation_wiring_state` gains a monotonic request counter and a paid
counter (recommended: `settle_requested_seq`, `settle_paid_seq`, plus
`settle_requested_at` and `settle_attempted_at` for ordering and age). A settle is owed
while requested is above paid. A pass requests one by incrementing the request counter
in one committed update, before it stamps the version and before the message is acked.
A crash between the skip and the request leaves the message unacked and the version
unstamped, so the redelivery repeats the pass.

A reconcile, a teardown, or a settle pass requests one when (a retry pass never does:
a row it lost is another pass's work, and the tick selects it again):

- its ledger's `lost` is not empty at the end of the pass (any refused park, release,
  claim, or failure write; this also covers a row that left `FAILED` under the pass's
  stale copy); or
- it is an L1 carried-delta apply and a released pair has a `FAILED` intended-`ACTIVE`
  row (D9).

**The settle pass.** A fourth step of `run_wiring_retry_tick`, after the three layer
retries, isolated the way each layer is (WIRE-RETRY-15). It selects up to
`WIRING_RETRY_BATCH_SIZE` reservations that owe a settle, least recently attempted
first, and for each:

1. reads the request counter as s and stamps the attempt time;
2. if the reservation's wiring is frozen, runs `_teardown_from_ledgers` again (it is
   idempotent, WIRE-TEARDOWN-5); otherwise asks reservations for the status, exactly as
   the corroboration gate does for a `wiring_changed` (WIRE-GATE-1), and on `ACTIVE`
   runs the full three-layer reconcile from cabling's current intended set. Any other
   answer, or no answer, leaves the mark for the next tick (fail closed);
3. if the pass raised nothing and its own `lost` is empty, sets paid to s by a
   compare-and-swap that only raises it. A request made after step 1 is above s and
   stays owed. A settle pass that skips requests again like any pass.

The full reconcile is the body of `handle_wiring_changed`'s full-reconcile branch plus
the L2 and L3 passes, factored into one function both callers use. The settle pass does
not stamp a fork version: it applies the fork's current intent, which is what a heal
does, and the ordering stream stays the events'. An event that arrives later for a
version the settle pass already applied is absorbed by the existing gates
(`is_pair_active`, `pair_needs_release`, and the L2 and L3 full diffs). An event-driven
full reconcile or teardown that skips nothing may pay the mark the same way.

No new setting: the settle pass runs wherever `WIRING_RETRY_ENABLED` runs the tick. A
deployment that disables the tick on every replica gets no settle pass, the same
condition under which FAILED rows are left to manual retry today.

**Attack: can it loop forever?** The mark is re-requested only by a skip, never by a
failure. A settle pass that drives and fails records `FAILED` rows and pays the mark;
those rows are the tick's (capped by `WIRING_RETRY_MAX_ATTEMPTS`). A skip needs a live
claim, or a row that moved since the pass's read, which the next pass reads fresh. So an
endless run of settle passes needs an endless run of claims on that reservation's rows.

**Attack: can it starve?** Let B be the claim budget and I the tick interval. One skip
is settled within B + I plus the pass's own duration, provided nobody claims the row
again. Each further claim adds at most B + I. The tick claims one row at most
`WIRING_RETRY_MAX_ATTEMPTS` times (10): a claim is followed by a drive, and a failed
drive adds at least one attempt (`_run_driver_with_retry` uses all three attempts before
it reports failure; an L3 switch records `switch_attempts or 1`). At the defaults that
is at most 10 x (300 + 60) seconds, one hour, for a row every automatic retry of which
collides with a settle pass; in a single replica the settle pass runs after the tick's
own drives have ended, so the tick's own claims have normally been cleared when it
looks. Two cases are not bounded by the system: manual retries (each press holds a row
for at most B), and a holder that dies mid-drive (its claim expires after B with no
attempt counted). After #1482 the L3 budget grows with the pin: (R + 2) driver actions
of 90.6 seconds worst case each for R route operations, rounded up to minutes, 19
minutes for R = 10. B in the bounds above is then the largest budget in play.

**Observability.** The tick line gains `settle_owed`, `settle_run`, `settle_paid`, and
the oldest owed age. A mark older than twice the largest budget plus two intervals logs
one WARNING with the fixed action `wiring_settle_overdue`. It is an alarm, not a stop:
giving up would break CONSERVATION.

Least-recently-attempted order is the #1190 lesson applied here: a batch of reservations
that keep skipping must not hide a newer one.

### D6. The holder's writes do not change

A retry holder's success and release flips stay unconditional, exactly as on main, and
so do the reconcile's now that it is a holder. This is the answer to the first question
of the issue's design comment: no new rule for the holder, because under D1 to D4 no
other pass changes a held row, so the holder finds the row as it left it. `holder_record`
is not revived in any form, and no write moves a row out of `ACTIVE` toward a rebuild.

`holder_or_unclaimed` (the L3 partial-failure write that also lands on a row whose claim
is null or past) stays until #1482 lands. Its stated reason, a claim cleared by a writer
that never claims, disappears with this record; what remains is an expired claim.

### D7. A pass that skipped an L2 membership releases no allocation

`_apply_l2_memberships` ends with `_release_orphaned_allocations` over the allocations
its removes touched. On main the reconcile drives every intended join itself, earlier
in the same apply, so a join that succeeds is `ACTIVE` before the orphan check. With a
skipped join that is no longer true: the pass could remove the last `ACTIVE` membership
and release the allocation while the holder is still joining a port to it, which is the
state issue #1413 already describes (an `ACTIVE` membership on a `RELEASED` allocation,
the VLAN undefined, the number free for another reservation).

So a pass whose ledger lost any L2 row of the reservation releases no allocation of that
reservation in that pass. The settle pass's L2 reconcile then runs the orphan check over
every `ACTIVE` allocation of the reservation, not only the ones its own removes touched;
the release is already a compare-and-swap on "no `ACTIVE` membership references it"
(WIRE-VLAN-4), so the wider check is safe. The skipped row's own allocation id is not a
reliable key for a narrower rule: a FAILED build row may carry a nil or stale allocation
until its retry re-resolves it (WIRE-RETRY-8).

### D8. L3: skip is per pin, teardown respects the claim, and #1482 comes first

An L3 row is one reservation's pin on one switch, and one claim covers the switch's
whole route session. So skip is all or nothing per (reservation, switch): no login, no
route call, no write. If more than one non-`RELEASED` row of the reservation exists for
the switch, the switch is skipped when any of them is held. Another reservation's pin on
the same switch is a different row and is not affected; L3 pins never displace each
other (WIRE-LEDGER-16). On L1 and L2 skip is per row: the pass still logs in and drives
the switch's other rows.

The teardown follows D1 and D3. Its park of a `FAILED` intended-`ACTIVE` pin
(`TEARDOWN_PENDING_REMOVAL`) runs under the guard. A held pin is skipped and the
teardown requests a settle; because the wiring is frozen by then (WIRE-TEARDOWN-1), the
settle pass re-runs the teardown, which is why D5 step 2 has a frozen branch: a
`wiring_changed` is a no-op on a frozen reservation and could never pay that mark.

Before the L3 phase ships, the L3 claim must cover its drive (issue #1482; its option
1 is assumed here, and its option 2 would serve as well). E1 holds only while a drive
ends inside its claim; an L3 pin with two or more route operations can outlive today's
budget, and a pass that respects the claim would then park or provision under a holder
that is still driving.

The residue read (`possibly_installed_routes`) moves after the claim, so a provision
never plans from routes another holder was still changing.

### D9. The L1 carried-delta path flags; it does not park

On the carried-delta path a released pair with a `FAILED` intended-`ACTIVE` row is left
as it is today, and the pass requests a settle. The settle pass's full reconcile then
judges the row against the fork's whole intended set and parks it under the guard. The
delta path gains no park site and no extra disconnect; the cost is one full reconcile,
within one interval, for a save that removes a wire whose build is failing.

Without the flag this path has a hole that the claim cannot see, because the reconcile
writes nothing: a retry pass validates intent, the owner removes the wire, the delta
apply passes over the row, the retry pass connects, and the row is `ACTIVE` for a wire
nobody intends with nothing owed. Read from `_reattempt_rows`, `_apply_wiring_pairs`,
and `record_l1_connect`; not reproduced.

### D10. Two reconcile-type passes of one reservation do not overlap in one replica

The settle pass adds a second way to start a reconcile in a process. The claim excludes
two passes on a row that exists as `FAILED`; it cannot exclude them on a key with no row
(a fresh build) or on an `ACTIVE` row (a release, an L3 route delta). A per-reservation
`asyncio.Lock` in the consumer module is taken by `handle_wiring_changed`, by the
terminal teardown, and by the settle pass. The settle pass does not wait for it: a
reservation whose lock is taken is left for the next tick. The consumer does wait, for
at most the rest of one settle pass of that same reservation, which is the same order
of time as the reconcile it is about to run; the in-progress signal keeps the message
from being redelivered meanwhile (WIRE-CONSUME-6). This is the one place the consumer
waits, and it never waits on a claim.

Across replicas two reconciles of one reservation can already overlap today:
`docs/ENV_VARS.md` documents a poller fleet (`EXECUTION_POLLER_ONLY`) in which every
replica runs the consumer, and `record_route_reconciled`'s docstring names the case.
This record does not close that; it stays a limit, and Open questions proposes where
it goes.

## Convergence, traced per layer

The case: a retry pass P holds row X and is mid-drive; the owner removes the intent; the
reconcile skips X and requests a settle; P finishes. Each row of the table is read from
the functions named, not executed.

| Layer | P's outcome | Ledger after P | What the settle pass does |
|---|---|---|---|
| L1 | success | X `ACTIVE` (`record_l1_connect` flips the FAILED row in place) | The key is `ACTIVE` and not desired, so it is on the release side (`ACTIVE` minus desired); `disconnect_ports`; `release_l1_connection` flips it `RELEASED`. A failed disconnect lands `FAILED` intended `RELEASED` for the retry channels (WIRE-STALE-4) |
| L1 | failure | X `FAILED` intended `ACTIVE`, stamp cleared by P's own write | The key is stale: guarded park, release side, `pair_needs_release` true, claim, `disconnect_ports`, `RELEASED`. The tick reaches the same park first if it runs first (WIRE-RETRY-6) |
| L1 | P dies | X `FAILED` intended `ACTIVE`, stamp expires | As failure, once the stamp is in the past |
| L2 | success | X `ACTIVE` (`record_l2_membership_active`) | (switch, port) is in `remove_keys`; `remove_from_vlan`; `release_l2_membership`; then the orphan check of D7 frees the allocation and undefines the VLAN when no `ACTIVE` membership is left |
| L2 | failure or death | X `FAILED` intended `ACTIVE` | Stale: guarded park and `remove_from_vlan`, or the direct release when the allocation is nil or unknown |
| L3 | success | X `ACTIVE` with the routes P drove (`record_route_active`) | The switch is in `remove_switches`; `remove_route` for the stored routes; `release_route_membership` |
| L3 | partial failure | X `FAILED` intended `ACTIVE`; routes are P's set plus residue it could not remove | Stale: guarded park, then `remove_route` for every stored route |
| L3, still adjacent, routing intent changed | success | X `ACTIVE` with P's older set | The switch is in `stay_switches` with a differing pin: the delta reconcile removes and adds, `record_route_reconciled` |
| L3 at teardown | success | The freeze branch of `record_route_active` parks X `FAILED` intended `RELEASED` with the union of stored and driven routes | Nothing left for the teardown re-run; the release-direction tick removes the routes while frozen (WIRE-TEARDOWN-4) |
| L3 at teardown | partial failure | X `FAILED` intended `ACTIVE` on a frozen reservation | The re-run teardown parks it `TEARDOWN_PENDING_REMOVAL` and deprovisions its stored routes (WIRE-TEARDOWN-6) |

The reverse case (P is removing, the owner re-adds the wire): the reconcile's build
loses the claim and skips. P succeeds and the row is `RELEASED`, so the settle pass sees
a desired key with no row and builds it fresh; P fails and the row is `FAILED` intended
`RELEASED` and unclaimed, so the settle pass claims it (it read that direction) and
builds over it, as the reconcile does today.

Where it does not converge, or converges only with a condition:

- **L2 allocation and VLAN definition.** Without D7 a skipped join can end `ACTIVE` on a
  released allocation. With D7 the membership and the allocation converge; issue #1413's
  original window (a join uncommitted at the instant of the release's update) is
  unchanged and stays open.
- **L3 route content after an expired claim.** If a claim expires mid-drive and another
  pass takes the pin, routes the superseded holder configured outside the recorded pin
  are recorded nowhere (the open cell of the issue's design comment). The settle pass
  compares pins to intent and cannot see them. Only #1482 prevents it.
- **A failed L1 or L2 build on a frozen reservation.** The row stays `FAILED` intended
  `ACTIVE`; the tick excludes it and the teardown does not read it (WIRE-TEARDOWN-6,
  last sentence). If the failed call did land on the device, nothing removes it.
  Existing decision, unchanged.
- **A frozen park keeps the attempt count** (WIRE-LEDGER-4). A row at or above the cap
  that the freeze branch parks is not selected by the tick; it is visible and waits for
  a manual retry. Existing behaviour, unchanged.

## Why this does not repeat the three failures

| Failure | Invariant it broke | Its precondition | Why it cannot recur here |
|---|---|---|---|
| Round 1: a guarantee narrower than stated | E2: writers outside the guarded set wrote a held row | Writers were guarded one by one, and some were missed | D1 is a rule about passes, not about writers: every pass owns a ledger and every failure write and settle refuses a row it does not hold, with the key branch as the backstop (D3). The guard test fails on a ledger writer or apply called without a ledger |
| Round 2: stamp guard on the holder's success flips | CONSERVATION: routes configured, record refused, nothing owed | A writer that never claims cleared the holder's stamp (the teardown park) | No pass clears a live stamp it does not hold any more (D1, D3, D8), so the trigger is gone; and the flips are not guarded at all (D6), so even a holder whose claim expired still records what it did. The teardown skips the held pin and owes a settle; both of P's outcomes are traced above |
| Round 3: `holder_record` PARK and REOPEN | E2 in effect: PARK wrote, by key, a row Y the holder never held, and released a wire the fork intends | Held row X was released during the drive and its key was rebuilt as a new row Y | While X is `FAILED` its key maps to X (D1, D2). A pass that wants to release X or build its key must claim X first and loses, so X is not released and Y is never created during the hold. Nothing decides from "the row as the holder finds it"; there is no PARK or REOPEN write |
| Main today (#1481) | E1 and E2 | The reconcile and teardown never claim | D1 |

The second comment on the issue (a frozen reservation, a parked pin whose deprovision
failed, then a holder's partial-failure write turning it back to intended `ACTIVE`)
needs the teardown to park a held pin, which D8 forbids, or a retry pass to claim a pin
parked before its claim, which D4 forbids.

## Invariants and what will pin them

Test names are proposals for the implementing pull requests; none exists yet.

| Invariant | To be pinned by |
|---|---|
| A reconcile over a row another pass holds changes nothing on it (`status`, `intended`, `attempts`, `last_error`, `claimed_until`) and makes no driver call for its key, per layer and per site of D3 | unit, `test_wiring_claim.py`: one test per site per layer; Postgres-live, one staged interleaving per layer |
| The key branch of each failure writer refuses a FAILED row with a live stamp that is not the caller's, and writes one whose stamp is null, past, or the caller's | unit, per layer, five claim states |
| A claim fails when `intended` changed since the pass's read, and the read-back reports `still_failed` for a row nobody holds | unit; Postgres-live for the park against claim order |
| A skip requests a settle before the version is stamped; a pass with no skip requests none | unit on `handle_wiring_changed` and `_teardown_from_ledgers` |
| Paying is a compare-and-swap on the request counter read at the start of the pass | unit; Postgres-live with a request staged during the settle pass |
| The settle pass runs the teardown on a frozen reservation, the full reconcile on an `ACTIVE` one, and nothing on any other or unreadable status | unit |
| A pass that lost an L2 row releases no allocation; the settle pass checks every `ACTIVE` allocation of the reservation | unit, `test_nats_consumer_vlan_definitions.py` pattern |
| No call of a ledger writer or of the three applies omits the ledger | AST scan, with its limits in its docstring |
| The holder's success and release flips are unchanged | the existing `test_a_superseded_holders_failure_is_refused_but_its_success_is_recorded` and `test_a_superseded_l3_holder_records_only_its_success` stay green |

## Options rejected

| Option | Complexity | What it covers | Why not |
|---|---|---|---|
| Issue option 1 alone: the parks keep `claimed_until` | Low | Effect 1 only | The key-based failure write still rewrites and frees a held row, and the reconcile still drives a held row's key. The owner rejected it. Its useful part is kept by D3: a parked row is claimed before it is driven |
| Issue option 3: the reconcile waits for the holder | Low | All three effects | The consumer handles one message at a time, so one held row would stall every reservation's events on that replica for up to a claim budget, terminal events included. The owner rejected it |
| Settle by delayed redelivery of the same event (the owner's first example) | Medium | The reconcile's skips | Four redeliveries per message, shared with real upstream failures; the default schedule spans 81 seconds against a 300 second claim; the fifth failing delivery dead-letters a healthy event, and a dead-lettered `wiring_changed` is not staged again, so at exhaustion CONSERVATION fails. It also cannot serve the teardown: deferring a terminal event risks dead-lettering it. A dedicated longer delay does not change the count |
| Settle by a follow-up event through execution's outbox (the owner's second example) | Medium | The reconcile's skips | The outbox has no delay, so the follow-up arrives while the row is still held and repeats every few seconds, each time a full reconcile. Execution would have to publish on a subject reservations owns, breaking RES-EVENT-2, or grow a new subject and consumer filter. A follow-up at a stamped version is a no-op, so the version would have to stay unstamped |
| Ask reservations to stage a heal | Medium | The reconcile's skips | A new internal route and a cross-service round trip for state execution holds itself; the same version problem; and it cannot pay a frozen reservation's mark |
| The holder settles what the reconcile skipped (`holder_record` again) | High | The reconcile's skips | Round 3. The holder would again decide from a row it cannot fully interpret |
| Filter held rows out of the reconcile's reads | Low | Effect 1 | Breaks E1: the key looks empty and the reconcile builds it fresh (D2) |

The trade the recommended mechanism makes: the settle pass costs one schema change and a
second caller of the reconcile body, and it delays the settlement of a skipped row by up
to one interval after the claim ends. In return it has no delivery count, survives a
restart, is the same idiom as the retry tick it runs in, and one mechanism serves the
reconcile and the teardown.

## Spec rule edits for the implementing pull requests

`docs/specs/provisioning-and-wiring.md`; not edited by this record.

- **Header.** Add ADR 0018 to the Design records row.
- **Section 4, claim stamp table.** The "any to null" row loses "and every key-based
  failure write and park the reconcile makes" and its guard "none"; those writes move to
  the guarded row, whose first column becomes "the writing pass's own stamp, null, or in
  the past" for every pass. The wiring state table gains the settle counters and their
  two transitions (request, pay).
- **Section 4, Concurrency.** "the two retry channels exclude each other with the claim
  stamp" becomes "every pass that drives a FAILED row excludes every other with the
  claim stamp".
- **WIRE-CLAIM-1.** Add: the update also requires the row's `intended` to equal the
  value the pass read.
- **WIRE-CLAIM-2.** "(a failure write by row id only when WIRE-CLAIM-8 lets it write)"
  becomes "(a failure write or a settle only when WIRE-CLAIM-8 lets it write)".
- **WIRE-CLAIM-4.** Proposed wording: "A row that exists as `FAILED` is claimed
  immediately before its own driver call (an L3 pin once, before its switch's login),
  never at selection, by whichever pass drives it: the retry tick, a manual retry, the
  reconcile, the terminal teardown, or the settle pass. A key with no `FAILED` row has
  nothing to claim."
- **WIRE-CLAIM-5.** "in that pass" stays; add that the pass requests a settle
  (WIRE-SETTLE-1) unless it is a retry pass.
- **WIRE-CLAIM-7.** Keep the rule; add the second reason of D2.
- **WIRE-CLAIM-8.** "a retry pass (tick or manual)" becomes "a pass"; the key branch
  backstop is added; the limit about the reconcile is removed; the limit about the
  holder's flips stays, reworded as a decision of this record.
- **WIRE-CLAIM-9.** Remove "or whose claim a writer that does not claim cleared".
- **New WIRE-CLAIM-10.** Every pass owns one `WiringRowClaims`; no ledger writer or apply
  is called without one.
- **WIRE-LEDGER-6.** A reconcile failure write for a key that has a `FAILED` row goes by
  row id; the key branch refuses a held row.
- **WIRE-LEDGER-13.** A park by any pass runs under the guard.
- **WIRE-STALE-1 to WIRE-STALE-3.** A held stale row is skipped and a settle is
  requested.
- **WIRE-ORDER-10.** The version is stamped even when rows were skipped; the settle
  request commits first.
- **WIRE-ORDER-4 or a new rule.** The carried-delta flag of D9.
- **WIRE-TEARDOWN-6.** The park runs under the guard; a held pin is skipped and settled
  by a re-run of the teardown.
- **WIRE-VLAN-4.** The deferral and the wider orphan check of D7.
- **WIRE-RETRY-16.** The settle counters in the tick line.
- **New section 8.16, WIRE-SETTLE-1 to WIRE-SETTLE-5.** Requesting; the settle pass's
  selection and order; what it runs by reservation state; paying; the overdue warning.
- **Section 13.** Remove the two #1481 limits; add the limits listed under Convergence
  and the cross-replica overlap of D10.

Also `CHANGELOG.md`, the module docstring of `wiring_claim.py` (it says the reconcile
never claims), and the docstrings of the three applies ("the reconcile path passes
neither").

## Test plan

At HERD's five levels.

1. **Unit** (SQLite, `services/execution/tests/`). The invariants table above. Existing
   tests that pin main and flip on purpose:
   `test_l1_key_upsert_failure_write_clears_the_claim`,
   `test_l1_stale_build_park_clears_the_claim`,
   `test_l2_stale_build_park_clears_the_claim`, the three
   `test_after_the_reconcile_clears_the_claim_*` tests (their premise, a reconcile
   clearing a live claim, becomes the thing that must not happen), and in the
   last phase `test_write_guard_does_nothing_without_a_ledger`.
   `test_claim_of_none_row_id_is_always_granted` stays: a key with no row is still
   drivable.
2. **Functional** (the service's real surface, in process). The internal retry route
   `POST /internal/reservations/{reservation_id}/wiring/retry` while a reconcile holds a
   row answers `in_progress` for it, a new reachable answer; the wiring status payload is
   unchanged. `handle_reservation_event` driven with a real payload for the skip, the
   stamp, and the mark.
3. **Postgres-live** (`test_wiring_retry_claim_race_live_pg.py` pattern:
   `_gated_driver_stub`, `_loser_inside_holders_drive`; every overlap staged with events,
   every wait bounded, no sleep). The interleavings:
   - a reconcile lands while a retry pass is inside its drive, once each for an L1
     connect, an L2 join, and an L3 provision: the driver is called once for the row, the
     row is unchanged until the holder writes, the mark is owed, the version is stamped;
   - the holder succeeds after the wire was removed: the settle pass releases the row,
     per layer, and for L2 the allocation is freed only then;
   - the holder fails after the wire was removed: the settle pass parks and releases;
   - the claim expires while the reconcile is skipping (the holder never returns; the
     stamp is moved into the past by the test, not waited for): the settle pass settles
     the row;
   - two reconciles in a row both skip: one settle pass pays both requests;
   - a request arrives during a settle pass: the mark stays owed;
   - a park and a build claim on one row, both orders: exactly one wins, and a build
     never follows a park;
   - a terminal teardown lands while a retry pass is inside an L3 provision, with both
     holder outcomes.
   The two live tests that pin main,
   `test_teardown_during_a_held_l3_provision_leaves_its_routes_recorded_for_removal` and
   `test_a_reconcile_failure_write_during_a_held_l1_connect_still_records_it`, are
   rewritten to the new behaviour. Each fixed test carries its mutation proof: the guard
   removed per site, the claim's `intended` predicate removed, the pay made
   unconditional, the request moved after the stamp.
4. **Integration** (running stack, mock drivers). The L1, L2, L3, and ledger teardown
   suites stay green; no API changes. One convergence test is possible with the mock
   drivers' `HERD_mock_sleep_ms` knob, asserting the end state by polling the wiring
   status, never by timing; it proves the settle pass runs in a real stack and is the
   only level that does. If that knob proves too coarse, the nightly's Postgres-live step
   is the stack-level proof, as it was for #1486.
5. **Load** and **browser end to end**: not applicable as new suites. There is no new
   route and no UI change: the retry summary already counts `in_progress` and words it
   "already retrying" (`frontend/src/api/reservations.ts`), which stays true of a row
   the reconcile is driving; only code comments that name the background retry channel
   change. The settle pass's cost when nothing is owed is one indexed select per tick,
   pinned by a unit test that counts statements, not by a wall-clock bound.
   `make test-load` and the nightly e2e run unchanged and must stay green.

## Delivery phases

Each phase is one pull request, mergeable alone, and leaves main correct.

0. **Issue #1482** (its own issue and decision). Required before phase 5.
1. **The settle mark and the settle pass, inert.** The migration, request and pay
   helpers, the reconcile body factored into one function, the settle step in the tick
   with its frozen branch, corroboration, order, stats, and the per-reservation lock of
   D10. Nothing requests a settle yet, so the step selects nothing.
2. **The claim reads `intended` (D4)** for the retry channels, with the read-back change.
   Closes the stale-build-plan hole between retry passes on its own.
3. **L1.** The reconcile owns a ledger and maps keys to rows; guarded park; the key
   branch backstop in `record_l1_failed`; the carried-delta flag (D9); the request
   before the stamp. L2 and L3 behave as on main.
4. **L2**, with D7.
5. **L3 and the teardown**, with D8.
6. **Clean-up.** Remove `claims=None` and its branches, add the AST guard, and remove
   `holder_or_unclaimed` if the owner agrees.

Every phase changes the spec rules, docstrings, and CHANGELOG lines it makes untrue in
the same pull request, as the repository requires; the list above under Spec rule edits
is the union.

Phases 3 to 5 each convert one layer; an unconverted layer keeps today's behaviour, so
the order among them is free. Phase 1 must precede them: a skip with no settle pass
would be the conservation defect this record exists to avoid.

## Consequences

- A save that touches a row a retry pass is driving no longer acts on that row at once;
  it is settled up to one interval after the claim ends. Everything else in the save is
  applied as before.
- A manual retry can now answer `in_progress` for a row the reconcile is driving, and a
  retry pass's claim can be lost to the reconcile.
- The reconcile makes one short update per FAILED row it drives and one locked select
  per park. Rows with no FAILED history cost nothing extra.
- One migration on `reservation_wiring_state`; no new setting, route, event, or subject.
- The reconcile body gains a second caller, and a reconcile can start from the retry
  loop. That is new concurrency in one process; D10 bounds it.
- Several tests that pin main's behaviour flip deliberately; the test plan lists them.

## Open questions for the owner

Each has this record's recommendation.

1. **The settle mechanism.** D5 recommends a durable mark and a settle pass in the retry
   loop, which is neither of the two examples in the starting position. Recommendation:
   accept D5; the delivery count makes redelivery unable to guarantee CONSERVATION, and
   the outbox has no delay and no subject of execution's own to publish to.
2. **Order against #1482.** That issue is not decided. Recommendation: decide it and
   land its option 1 before phase 5; until then the L3 guarantee is "within the budget".
3. **D4, the claim reading `intended`.** It changes `WiringRowClaims.claim` for the
   retry channels too. Recommendation: yes; without it a successful park is not final,
   and the hole exists on main between two retry passes.
4. **D7 and issue #1413.** D7 is derived from reading `_apply_l2_memberships` and
   WIRE-VLAN-4; the fix shape chosen for #1413 may make it simpler or redundant.
   Recommendation: adopt D7 as written unless #1413 lands first, then re-read it.
5. **Overlapping reconciles.** D10 serializes within one process only, and makes the
   consumer wait for a settle pass of the same reservation. A reservation-level claim
   stamp on `reservation_wiring_state` (a reconcile that cannot take it requests a
   settle and acks) would cover the documented poller fleet in the same idiom, with no
   wait, but it sends more work through the new settle pass. Recommendation: the lock
   in phase 1, and a separate issue for the cross-replica overlap, which exists today
   whatever this record decides.
6. **`holder_or_unclaimed` after #1482.** Recommendation: remove it in phase 6; once no
   pass clears a stamp it does not hold and the budget covers the drive, the strict
   stamp is always the holder's.
7. **At most one non-`RELEASED` row per key.** The traces assume it. It holds through
   the keyed writers in one process, but no index enforces it for `FAILED` rows, and two
   replicas inserting a failure for one key at once could create two. Recommendation:
   out of scope; file it separately. D8's "any of them is held" rule is written so L3
   stays safe either way.
8. **Should a holder that finds its stamp gone at its success flip request a settle?**
   It would let an expired-claim double drive converge on L1, L2, and L3 adjacency (not
   on L3 route content). Recommendation: defer; decide after #1482, when the case should
   be unreachable.
9. **A stuck mark.** A reservation that reservations never reports `ACTIVE` again and
   that execution never freezes keeps its mark, with the overdue warning. Recommendation:
   accept; it means a terminal event was lost, which is its own defect.

## Revisit when

- the consumer handles more than one message at a time, or a deployment relies on
  several consumer replicas for wiring throughput (D10's limit becomes the common case);
- a driver call can outlive its claim by design (a new layer, or a driver action with no
  timeout), which removes the premise of E1;
- the retry tick is disabled as a supported configuration, since the settle pass lives
  in it;
- issue #1413 changes how an allocation is released (D7);
- a fourth ledger is added: it needs the same three things, a claim before the drive, a
  guarded settle, and the settle request.
