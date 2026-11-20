# Decision: The Wiring Reconcile Respects the Drive Claim, Issue #1481

Status: Proposed 2026-11-20, revision 2 (after an adversarial review of revision 1,
commit `6a6b912e`). The owner chose option 2 of issue #1481: the reconcile takes a
drive claim, skips what a retry pass holds instead of waiting, and what it skipped is
settled later. This revision keeps those three commitments and changes one thing the
owner has not ruled on: the claim is taken per reservation (a drive lease), not per
row. Revision 1's row-granular design did not survive review, and Options rejected
records what it would need to be sound. That change of granularity is open question 1.
Deciders: the owner. No code in this document. Context verified by reading the
HERD-public tree at `9a231984` on 2026-11-20; symbols are the stable reference and line
numbers are as of that commit. Three behaviours of main cited below (R1, R2, R3) were
reproduced during the review with SQLite tests kept outside the repository. Every other
trace is a reading of the code, not an execution, and the test plan names what has to
prove it. This area has now been designed wrong four times, so each trace says which
kind it is.

## Smallest safe first step

The owner may stop after any phase. Each row says what is closed and what stays as it
is on main today.

| Stop after | What ships | Closes | Stays open |
|---|---|---|---|
| Phase 1 | One predicate on the existing row claim: it also requires the row's `intended` to equal what the pass read. No schema | R1: a retry pass build-drives a row another pass parked | The three effects of #1481; R2; R3; overlapping reconciles across replicas |
| Phase 2 | The reservation drive lease, the settle mark, and the settle step, used by the reconcile, the teardown, and the settle step only | Two reconciles, or a reconcile and a teardown, driving one reservation at once from two replicas | The three effects of #1481; R2; R3 (retry passes still run outside the lease) |
| Phase 3, per layer | Retry passes take the lease and plan under it | For that layer: the three effects of #1481, R2, R3. With all three layers, #1481 is closed | The row claim is still present, now redundant; a dead L3 retry holder still blocks retries of its pin for the row budget of PR #1490 |
| Phase 4 | The row claim is removed | A dead holder frees its reservation within the lease's two minutes, whatever the route count | Only the limits listed under "Where it does not converge" |

Phase 1 was confirmed safe alone by the review. It is worth shipping whatever the owner
decides about the rest, because R1 is reproduced and the change is one predicate. It
does not close R3 and is not a substitute for phase 3.

## Context

### The claim, and who honours it today

A wiring ledger row (`l1_connection_assignments`, `l2_port_assignments`,
`route_assignments`) carries `claimed_until`. A retry pass takes the claim with one
compare-and-swap immediately before the row's own driver call
(`WiringRowClaims.claim` in `services/execution/app/services/wiring_claim.py`): the
update matches the row id, status `FAILED`, and a stamp that is null or in the past,
and writes now plus a budget (5 minutes at the defaults; PR #1490, for issue #1482,
sizes an L3 pin's budget from its drive sequence). Issue #817 added it so the retry
tick and the manual retry never drive one row at once. Issue #1479 (PR #1486) made
every failure write and every settle a retry pass makes respect it
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
  tick's work on one reservation, a manual retry, or the settle pass this record adds.
- The **lease** is one reservation's drive lease (D1). Its **holder** is the pass whose
  token is on it while it is live.
- To **defer** is what a consumer pass does when it meets a live lease: it records that
  a settle is owed and returns without doing its wiring work.
- A row's **key** is (switch, canonical port pair) for L1, (switch, port) for L2, and
  (switch) for L3, always within one reservation.

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

### What the code shows that the issue does not

1. The reconcile is not uniformly blind to the claim: `release_unapplied_route_pin`
   already leaves a held pin alone, silently.
2. The L1 stale park exists only on the full-reconcile path. On the carried-delta path
   a released pair whose row is `FAILED` intended `ACTIVE` is not parked:
   `pair_needs_release` answers false and `_apply_wiring_pairs` moves on. L2 and L3
   always run the full reconcile (WIRE-ORDER-8).
3. The issue says that after #1479 the retry holder no longer undoes the reconcile's
   park. That holds for the holder's failure writes other than the L3 partial failure.
   Its success flip is unconditional and flips the parked row `ACTIVE`, or inserts a
   new `ACTIVE` row when the reconcile already released it; the L3 partial-failure
   write also lands (`holder_or_unclaimed`). `test_wiring_claim.py`
   (`test_after_the_reconcile_clears_the_claim_the_holders_success_is_recorded`,
   `test_after_the_reconcile_clears_the_claim_an_l3_partial_failure_is_recorded`) pins
   both.
4. **R1, reproduced.** The claim does not check direction. A retry pass that loaded a
   row as a build, and whose row another pass then parked intended `RELEASED`, wins the
   claim and connects; the row ends `ACTIVE`.
5. **R2, reproduced.** A retry pass validates intent, the owner removes the wire, the
   carried-delta apply passes over the row and stamps the version, the retry pass
   connects, and the row is `ACTIVE` for a wire nobody intends with nothing left to
   release it.
6. **R3, reproduced.** A plan can be stale in content, not only in direction. The tick
   plans an L3 provision of routes r0; a newer reconcile provisions the same pin with
   r1 and fails, leaving it `FAILED` intended `ACTIVE` with r1; the tick claims the pin
   and pins r0 `ACTIVE`. No retry channel reads `ACTIVE` rows. The same class exists
   for an L2 row planned with a stale `vlan_assignment_id`.

R2 and R3 show that the defect is wider than "the reconcile ignores the claim": a pass
acts on a plan that another pass has since made untrue. A claim on the row closes that
only if the plan is made after the claim.

### Facts about the consumer and the replicas

- One durable pull consumer, `max_deliver` 5, no `backoff` (`start_nats_consumer`,
  `NATS_MAX_DELIVER`). A transient failure is `msg.nak(delay=...)` with the schedule
  `NATS_NAK_BACKOFF_SECONDS`, default `1,5,15,60,120` (`services/execution/app/config.py`,
  `nak_delay` in `services/common/herd_common/jetstream.py`). The fifth failing delivery
  is dead-lettered and acked (`process_reservation_message`): at most four redeliveries
  over 1 + 5 + 15 + 60 = 81 seconds.
- A dead-lettered `wiring_changed` is not staged again: reservations' sweep heals only
  when cabling's fork version is above its own `fork_wiring_ledger`
  (`_heal_wiring_staging` in `services/reservations/app/tasks/expiration.py`).
- The loop fetches one message per pull and handles it to completion (`_consumer_loop`,
  WIRE-CONSUME-2). A handler that waits stalls every reservation's events on that
  replica, terminal events included.
- Every replica starts the consumer (`lifespan` in `services/execution/app/main.py`);
  the retry tick starts wherever `WIRING_RETRY_ENABLED` is true. `docs/ENV_VARS.md`
  documents a poller fleet (`EXECUTION_POLLER_ONLY`). So two reconciles of one
  reservation, a reconcile and a teardown, or two ticks can run at once in different
  processes today, and anything exclusive has to live in the database.
- Every wiring driver call goes through `_run_driver_with_retry`, and the ledger writers
  are reached only from four entry points: `handle_wiring_changed`,
  `_teardown_from_ledgers`, `run_wiring_retry_tick`, and `reattempt_reservation` (a grep
  for the writers finds callers only in `nats_consumer.py` and
  `wiring_retry_service.py`).
- `reservation_wiring_state` may have no row for a reservation (`get_wiring_state`
  returns None; `stamp_last_applied` and `freeze_reservation_wiring` insert it).
- A redelivered event whose version was stamped is a no-op (WIRE-ORDER-3). An event at
  exactly one above the last applied version with a delta is applied as that delta, not
  as a full reconcile (WIRE-ORDER-4).

### Four attempts that failed

| Attempt | What it did | What broke |
|---|---|---|
| #1479 round 1 | Added the claim condition to the retry failure writes by row id only | The guarantee was narrower than stated: a retry pass's settles on rows it never claimed, and a holder whose claim had expired and been retaken, still wrote (commit `f5a98551`) |
| #1479 round 2 | Put the holder's success and release flips behind the stamp too | A writer that never claims (the teardown park) cleared the stamp during a held L3 provision; the holder's success was refused and the configured routes stayed on the switch with no ledger row (commit `fd9ccbf2`) |
| #1479 round 3, `holder_record` (as of commit `a8b14242`) | The holder decided from the row as it found it: PARK an addition that landed on a row settled toward release, REOPEN a removal that landed on a row rebuilt `ACTIVE` | PARK wrote by key. When held row X had been released during the drive and the key rebuilt as a new row Y, PARK flipped Y to `FAILED` intended `RELEASED`, and the release channel would tear down a wire the fork intends (commit `b8e728b2`) |
| This record, revision 1 (commit `6a6b912e`) | The reconcile took the row claim, skipped held rows inside its pass, and owed a settle paid by a step in the retry tick | A row that is `ACTIVE`, or a key with no row, cannot be claimed, so two settle passes on two replicas both drove it; a pass that skips part of a reservation leaked a VLAN allocation, could loop, and requested its settle too late; the claim compared direction but not content (R3); an in-process lock stalled the consumer |

`a8b14242` is in main's history: the #1486 branch was merged by a merge commit, and
`b8e728b2` removed `holder_record` later on the same branch.

Rounds 2 and 3 tried to make the holder cope with a row another writer had changed
under it. Revision 1 removed that writer for one row at a time, and every defect the
review found came from the same place: a pass that does part of a reservation's work
while another pass does another part. This revision makes a pass all or nothing for
its reservation.

### The two invariants

- **EXCLUSION.** (E1) At most one pass has a wiring driver call in flight for one
  reservation. (E2) A ledger write for a reservation's rows is made only by the pass
  that holds its lease. Two exceptions to E2 are kept from main on purpose: a pass
  always records an action it made that changed the device (the unconditional success
  and release flips, and the L3 partial-failure write), and the two cross-reservation
  writers still flip another reservation's `ACTIVE` row (WIRE-LEDGER-11,
  WIRE-LEDGER-12).
- **CONSERVATION.** No intended change is dropped. Every `wiring_changed` and terminal
  event that reaches its handler is either applied by the pass that received it or
  leaves an owed settle in the database before the message is acked. An owed settle is
  paid only by a full pass that held the lease and completed. After such a pass every
  row is `ACTIVE` and intended, `RELEASED`, `FAILED` in the retry channels' hands, or
  terminal by an existing decision (a pinned reason, a row at the attempts cap, a
  failed L1 or L2 build on a frozen reservation).

## Decision

### D1. One drive lease per reservation, and every pass holds it

`reservation_wiring_state` gains a lease: a token and an expiry. A pass takes the
reservation's lease before it reads anything it plans from, holds it until its last
write, and releases it. Every kind of pass takes it: the `wiring_changed` apply, the
terminal teardown, the settle pass, the retry tick for each reservation it works on,
and the manual retry.

The reservation is the smallest unit that can be claimed for everything a pass does.
A row claim cannot cover a release of an `ACTIVE` row or a build of a key that has no
row yet, and those are exactly the drives two reconciles collide on.

The reconcile path stays as it is on main inside the lease: no row claim, no key-to-row
map, no guard on its parks or its key-based failure writes. The lease is what makes
those safe. The retry channels keep their row claims untouched until phase 4; under the
lease they are redundant, not wrong.

The keyed writers assume one non-`RELEASED` row per key, and no index enforces that for
`FAILED` rows. Under the lease two passes cannot insert a row for one key of one
reservation at once: from phase 2 for the key branch of the failure writers, which only
the consumer's passes use (a retry failure write goes by row id and has no insert
branch), and from phase 3 for every insert. So the assumption becomes a consequence,
with no schema change.

### D2. The lease: taking, renewing, losing

- **Take.** One upsert on `reservation_wiring_state` (the row may not exist) that
  writes the pass's fresh token and an expiry one lease term ahead, and matches only
  when the row is absent or its expiry is null or not in the future. One row written
  means taken.
- **Renew.** One update that moves the expiry one term ahead where the token is still
  the pass's own. No row written means the lease was lost.
- **Release.** One update that clears the expiry where the token is the pass's own, in
  a `finally`.
- **No in-process belief.** A lease object is made per pass per reservation with a
  fresh token. Taken, renewed, and lost are each decided by a statement's row count;
  nothing is answered from memory, unlike `WiringRowClaims.claim`, which answers True
  for a row already in its `held` map. The settle step builds no `WiringRowClaims`.
- **Where a pass renews.** Before every driver attempt and before every upstream read,
  inside the two functions every such step already goes through:
  `_run_driver_with_retry` and `_get_internal` (and the fabric lookup in
  `vlan_service.py`). The lease travels in a context variable, so no apply signature
  changes; the consumer's task, the tick's task, and a request's task each have their
  own context. `_run_driver_with_retry` refuses a wiring driver call with no lease in
  context, which makes "no wiring driver call outside a lease" one check at one choke
  point instead of a rule about many writers. A driver attempt starts only when a
  renewal committed within a short gap, measured on the holder's monotonic clock. A
  ledger write that records no device change (a failure write, a park, a release with
  no driver call) renews first and is not made when the renewal fails.
- **The term.** Sized for one step, not for a pass: the longest of one driver attempt
  (`EXECUTION_TIMEOUT_SECONDS`, plus the 60 second `pip install` when
  `ALLOW_DRIVER_PIP_INSTALL` is on) and one upstream read (10 seconds), plus the gap
  and an allowance for the kill and reap, rounded up to whole minutes: 2 minutes at the
  defaults. It is an allowance, not a measured bound, the same honesty PR #1490 uses. A
  pass of any length keeps the lease by renewing; a holder that dies frees its
  reservation within one term, whatever the reservation's size.
- **Losing it.** A renewal that writes no row raises out of the pass. The pass makes no
  further driver call, failure write, or park. A success or release flip for an action
  already made is written before the next renewal point, as the code orders it today.
  L3 needs one addition, because it records a switch only after its whole route
  sequence: a lost lease stops the sequence, and the pass writes what it may have
  installed (the partial-failure write) before it leaves. A consumer pass that loses
  the lease defers (D4); a retry pass stops and its rows read back by status.
- **The clock.** The expiry is written and compared by the database, in the statement,
  with `clock_timestamp()` and not `now()` (which is the transaction's start and can be
  stale in a session that has already read). No process compares a stamp with its own
  wall clock, so the design tolerates any wall-clock offset between replicas. It does
  not tolerate one step outlasting the term (a stall between a renewal and the end of
  the attempt it covers), or the database clock jumping forward by more than the term's
  slack; either lets a second pass in for the length of one driver attempt.

### D3. A pass plans under the lease

Every read a pass plans from is made after it took the lease: the wiring state row
(frozen, last applied version), the fork's intended set, and the ledger rows. A retry
pass re-reads the rows it selected, by id, after it takes their reservation's lease; a
row that is no longer `FAILED` is dropped, and the pass uses the `intended`, `routes`,
and `vlan_assignment_id` it re-read, then validates build intent against the fork as it
does today.

This is what closes R1 and R3, and it is why a claim that only compares direction is
not enough: nothing a pass decided before the lease is used after it.

### D4. A pass that meets the lease never waits

| Pass | When another pass holds the lease |
|---|---|
| `wiring_changed` apply | After the frozen check and the stale-version check (both read values that only move one way, so they are safe to read without the lease): requests a settle carrying its fork version (D5), does not stamp, returns. The message is acked |
| Terminal teardown | Freezes first, as today (WIRE-TEARDOWN-1), then requests a settle and skips the ledger teardown. Dynamic-instance teardown runs as today. The message is acked |
| Settle pass | Passes over the reservation. Requests nothing, writes nothing |
| Retry tick | Passes over the reservation's rows: they stay `FAILED`, no attempt is counted, and they count `in_progress`. Its three selects leave out rows of a reservation whose lease is live, so such rows do not fill a batch (the #1190 lesson) |
| Manual retry | After the `not_retryable` and `frozen` classification (WIRE-RETRY-11), reports the reservation's other rows `in_progress` |

Only the first two request a settle, and each does it once, before any wiring work
(the teardown after its freeze). So a request exists for a deferred event and for
nothing else: a settle pass that meets a lease cannot request another, which is what
makes the loop the review found in revision 1 impossible.

The retry channels also pass over a reservation that owes a settle whose attempts have
not failed (D6), for at most the overdue threshold of D8. The settle pass does a full
reconcile, which rebuilds and releases everything a retry would; letting retries take
the lease first would only delay it.

### D5. The settle mark

`reservation_wiring_state` gains a request counter, a paid counter, the largest fork
version among deferred events, a failure count, and the time of the last attempt
(recommended names: `settle_requested_seq`, `settle_paid_seq`, `settle_fork_version`,
`settle_failures`, `settle_attempted_at`). A settle is owed while requested is above
paid. A partial index on that predicate keeps the settle step's select to the owed
rows.

A request is one upsert (the state row may not exist) that raises the request counter
and the deferred version, committed before the handler returns. A deferred pass does
no wiring work before it, so there is no moment at which a pass has skipped work and
could still raise without having recorded it.

### D6. The settle step

**Where it runs.** In the consumer's own loop (`_consumer_loop`), after each fetch
whatever its outcome: at most one settle pass between fetches. Three consequences:

- the consumer never waits for a settle pass, because it is the one running it, between
  messages, on its own task; there is no second task and no in-process lock;
- a settle step runs in every process that can make a request, since only consumer
  passes request; it does not depend on `WIRING_RETRY_ENABLED`;
- when idle it runs within `NATS_FETCH_TIMEOUT_SECONDS` (5 seconds) of the request.

**What it does.** It selects one reservation that owes a settle, is not parked, and
whose spacing has elapsed, least recently attempted first, with `FOR UPDATE SKIP
LOCKED`; then takes the lease (a held lease: pass over). Under the lease it reads the
request counter as s, the deferred version as m, and the freeze, then:

1. frozen: runs `_teardown_from_ledgers` (idempotent, WIRE-TEARDOWN-5);
2. not frozen: runs the full three-layer reconcile from cabling's current intended set,
   the body of `handle_wiring_changed`'s full-reconcile branch with the L2 and L3
   passes, factored into one function both callers use.

**Paying.** When the pass completes it stamps the last applied version (below), sets
paid to s by an update that only raises it, and clears the failure count. A request
made during the pass is above s and stays owed. An event-driven full reconcile or
teardown that completes under the lease pays the same way, with the s it read under the
lease. A carried-delta apply pays nothing.

**The version.** A deferred event is not stamped, so a later event above it is not
contiguous and takes the full-reconcile path (WIRE-ORDER-4). When a full pass
completes it stamps the largest version it can prove it applied, through
`stamp_last_applied`, which never lowers: m (and its own version, for an event),
because the fork was read after every deferred event up to m was staged. A nacked
event at or below m then arrives stale and is a no-op (WIRE-ORDER-3).

That is the real mechanism, and it has one remaining gap, which is main's today and not
new: a full reconcile reads the fork as it is now, which can be above the version it
stamps. An event between the stamp and the fork's version then arrives contiguous and
its carried delta is applied over newer state; if a later version removed a wire that
delta built, the wire is rebuilt until the later event lands. A gap or heal reconcile
on main has the same exposure. Stamping the fork's own version would close it for all
three; open question 3.

**When the pass raises.** The failure count goes up, the lease is released, and the
mark stays owed. Attempts are spaced, doubling from `WIRING_RETRY_INTERVAL_SECONDS` to
the retry loop's cap. After `NATS_MAX_DELIVER` (5) consecutive failures the settle of
a reservation that is not frozen is parked: no automatic attempt, one ERROR line with
the fixed action `wiring_settle_parked`, the retry channels resume, and any later full
pass under the lease (the next save, or the teardown once the reservation ends) pays
it. This is the dead-letter analogue, reached only by a
pass that keeps failing, never by waiting. Without it a reconcile that fails on one
switch's inventory read would re-drive another switch's failing rows on every attempt,
with no attempts cap. A frozen reservation's settle is never parked: a teardown drives
only `ACTIVE` rows, and each failure turns its row `FAILED` for the capped retry
channel, so repeating it cannot loop on a row.

### D7. The holder's writes do not change, and the row claim stays for now

A holder's success and release flips stay unconditional, exactly as on main. No other
pass changes a reservation's rows during the hold, so the holder finds each row as it
left it. `holder_record` is not revived in any form, and no write moves a row out of
`ACTIVE` toward a rebuild.

`WiringRowClaims`, `claim_write_clauses`, `write_guard`, `holder_or_unclaimed`, and the
L3 budget of PR #1490 are not touched before phase 4. PR #1490 fixes a defect on main
and merges on its own; this record neither needs it nor conflicts with it. Revision 1
moved the L3 residue read behind the row claim, while PR #1490 sizes that claim's stamp
from the sequence built before it; that collision is gone, because the residue read
stays where PR #1490 has it, no lease stamp is sized from a drive sequence, and from
phase 3 no other pass can change the pin between the read and the row claim.

### D8. Bounds and alarms

- **A dead holder** frees the reservation within one lease term: 2 minutes at the
  defaults, independent of how many rows or routes it was driving. Until phase 4 a dead
  L3 retry holder also leaves its pin's row claim standing for the row budget, which
  blocks retries of that pin (not the reconcile, not the settle pass): 19 minutes for
  10 route operations at the defaults, and unbounded in the route count until issue
  #1493's limit.
- **A live holder** holds the lease for its whole pass. That has no constant bound: a
  pass is one login, one logout, and one action per row or route for each switch, each
  action at most 90.6 seconds at the defaults. One L3 pin of 10 route operations is at
  most about 18 minutes; with #1493's proposed limit of 1,000 routes, about 25 hours.
- **A deferred event** is applied by the first full pass that takes the lease after
  the holder ends. While the settle is owed and its attempts have not failed, the retry
  channels pass over the reservation. An event that takes the lease first is either a
  full reconcile, which pays the mark, or one carried delta, which leaves it owed; each
  is one event, so neither can starve the settle step. The delay is the holder's
  remaining pass (or one lease term if it died), plus one iteration of some replica's
  consumer loop (5 seconds when idle, or the message it is handling). There is no
  smaller true bound, and the bound is per reservation, which is the unit a settle
  pays.
- **No loop.** A settle is requested only by a deferred event or teardown, and a settle
  pass never requests, so the settle passes that complete are at most the deferred
  events. A pass that fails is tried again at most five times on a reservation that is
  not frozen, and without limit on a frozen one, where no row is driven twice (D6).
- **Alarms.** The retry tick, which runs on a different loop from the settle step,
  reports `settle_owed`, `settle_parked`, and the oldest owed age in its tick line, and
  logs one WARNING with the fixed action `wiring_settle_overdue` for a mark that has
  been owed for more than ten minutes while its reservation's lease is free: owed, and
  nobody is working on it. Age alone is not the test, because a long L3 drive
  legitimately holds a lease for longer. A lease renewed by one token for more than
  thirty minutes logs `wiring_lease_long_held` once, at INFO.

## Traces

All read from the functions named, not executed, unless marked reproduced.

### The owner removes the intent while a retry pass is driving

Retry pass P holds the lease and is mid-drive on row X. The save's event meets the
lease, requests a settle, and is acked. P finishes and releases. The settle pass takes
the lease and runs the full reconcile.

| Layer | P's outcome | Ledger after P | What the settle pass does |
|---|---|---|---|
| L1 | success | X `ACTIVE` (`record_l1_connect`) | The key is `ACTIVE` and not desired: release side of the diff, `disconnect_ports`, `release_l1_connection`. A failed disconnect lands `FAILED` intended `RELEASED` for the retry channels (WIRE-STALE-4) |
| L1 | failure, or P dies | X `FAILED` intended `ACTIVE` | The key is stale: `park_stale_l1_build`, release side, `pair_needs_release` true, `disconnect_ports`, `RELEASED` |
| L2 | success | X `ACTIVE` (`record_l2_membership_active`) | In `remove_keys`: `remove_from_vlan`, `release_l2_membership`; then `_release_orphaned_allocations` frees the allocation when no `ACTIVE` membership is left, as on main |
| L2 | failure, or P dies | X `FAILED` intended `ACTIVE` | Stale: park and `remove_from_vlan`, or the direct release when the allocation is nil or unknown |
| L3 | success | X `ACTIVE` with the routes P drove | In `remove_switches`: `remove_route` for the stored routes, `release_route_membership` |
| L3 | partial failure | X `FAILED` intended `ACTIVE`; routes are P's set plus residue it could not remove | Stale: `park_stale_route_build`, then `remove_route` for every stored route |
| L3, still adjacent, routing intent changed | success | X `ACTIVE` with P's older set | In `stay_switches` with a differing pin: the delta reconcile, `record_route_reconciled` |
| Any, at teardown | any | The teardown was deferred; P's success after the freeze is parked `FAILED` intended `RELEASED` by the record-time freeze branch (WIRE-LEDGER-4) | Frozen: `_teardown_from_ledgers` releases every `ACTIVE` row and parks and deprovisions every `FAILED` intended-`ACTIVE` pin (WIRE-TEARDOWN-6); the release-direction retries finish the rest |

### The review's traces against this design

- **Two settle passes, two replicas, an `ACTIVE` row for a removed wire.** A takes the
  lease; B's take writes no row, so B passes over and requests nothing. A disconnects,
  releases, pays. B's next step finds nothing owed. One driver call.
- **The endless settle loop.** A's settle pass drives a failing row for 270 seconds. B
  meets the lease and does nothing: no `lost` set exists to turn into a request. A
  records the failure and pays; the row is the retry tick's, under its cap.
- **A settle pass against a terminal teardown.** The teardown holds the lease for its
  whole pass; a settle pass on another replica that sees frozen cannot take it. If the
  settle pass got there first, the teardown defers and the settle pass, on a frozen
  reservation, runs the teardown.
- **The allocation leak.** Allocation A has `ACTIVE` membership M1 and a `FAILED` join
  X that P is retrying. The owner removes both wires: the event is deferred whole, so
  M1 is not removed while P joins. P fails. The reservation completes: the teardown
  takes the lease, removes M1, and `_release_orphaned_allocations` frees A in that same
  pass, because A is among the allocations its removes touched. The owed wiring settle
  then runs on a frozen reservation, finds nothing `ACTIVE`, and pays. If the settle
  pass runs before the reservation ends, its own full reconcile removes M1 and X and
  frees A. No pass removes a membership without also running the orphan check for its
  allocation, because no pass is partial.
- **R1.** P re-reads X under the lease, finds it intended `RELEASED`, and treats it as
  a release-direction row.
- **R3.** The tick takes the lease, re-reads the pin (routes r1) and the fork's intent
  (r1), and drives r1. If the tick held the lease first and drove r0, the save of r1 is
  deferred and the settle pass's delta reconcile moves the pin to r1.
- **R2.** The carried-delta apply meets P's lease and is deferred; the settle pass
  releases the row P left `ACTIVE`.
- **The nacked version.** Last applied is 3; v4 is nacked for an upstream failure; v5
  is deferred. The settle pass applies the fork at v5 and stamps 5; v4 arrives stale
  and is a no-op. If instead v4 had been deferred and v5 nacked, the stamp is 4 and v5
  arrives contiguous, its delta a set of no-ops against state already at v5; the
  exposure that remains is the one named under D6.

### Where it does not converge, or converges only with a condition

- **A parked settle on an active reservation** (five failed attempts). The deferred
  save's changes are not applied until the next save or an operator acts. Visible in
  the tick line and the ERROR log. The same class as a dead-lettered event today.
- **One step outlasting the lease term.** A second pass can take the lease and drive
  the same reservation for the length of one driver attempt. On L3 the two passes'
  routes can then differ, and routes the superseded holder configured outside the pin
  the new holder records are recorded nowhere (the open cell of the issue's design
  comment). The lease term's allowance is the only defence.
- **The stamp below the fork's version** (D6): a wire can be rebuilt until a later
  event lands. As on main.
- **A failed L1 or L2 build on a frozen reservation** stays `FAILED` intended `ACTIVE`;
  if the failed call did land on the device, nothing removes it (WIRE-TEARDOWN-6, last
  sentence). Existing decision.
- **A frozen park keeps the attempt count** (WIRE-LEDGER-4): a row at the cap waits for
  a manual retry. Existing behaviour.
- **The cross-reservation writers** (WIRE-LEDGER-11, WIRE-LEDGER-12) act on another
  reservation's `ACTIVE` row without its lease, as today, and two reservations' passes
  can drive one switch at once, as today. The lease is per reservation.

## Why this does not repeat the failures

| Failure | Invariant it broke | Its precondition | Why it cannot recur here |
|---|---|---|---|
| Round 1: a guarantee narrower than stated | E2: writers outside the guarded set wrote | Writers were guarded one by one, and some were missed | The lease is not a property of writers. Every writer is reached from four entry points, each of which takes the lease first, and every wiring driver call passes one function that refuses to run without it |
| Round 2: stamp guard on the holder's success flips | CONSERVATION: routes configured, record refused | A writer that never claims cleared the holder's stamp (the teardown park) | The teardown cannot run while a retry pass holds the lease; it defers and is run by the settle pass afterwards. The flips are not guarded at all (D7) |
| Round 3: `holder_record` PARK and REOPEN | E2: PARK wrote, by key, a row Y the holder never held | Held row X was released during the drive and its key rebuilt as Y | No other pass writes the reservation's rows during the hold, so X is not released and Y is not created. Nothing decides from "the row as the holder finds it" |
| Revision 1: row claims plus a settle mark | E1 across replicas; CONSERVATION for allocations and for a request made too late | A pass skipped some rows and went on with others | A pass is all or nothing for its reservation: it holds the lease or it defers before doing anything |
| Main today (#1481, R2, R3) | E1, E2, and a plan made before the claim | The reconcile and teardown never claim; retry plans predate the claim | D1 and D3 |

The second comment on the issue (a frozen reservation, a parked pin whose deprovision
failed, then a holder's partial-failure write turning it back to intended `ACTIVE`)
needs a teardown and a retry pass on one reservation at once, which D1 excludes.

## Invariants and what will pin them

Test names are proposals for the implementing pull requests; none exists yet.

| Invariant | To be pinned by |
|---|---|
| Two takes of one lease: exactly one writes a row; an expired lease is retaken; a missing state row is inserted by the take | unit; Postgres-live with two connections |
| A renewal by a superseded token writes no row, and the pass then makes no driver call, failure write, or park | unit, with a lease retaken between two attempts |
| No wiring driver call without a lease in context | unit on `_run_driver_with_retry`; an AST scan that every caller of the three applies is one of the four entry points, with its limits in its docstring |
| The expiry is written and compared with the database clock | Postgres-live: a process whose own clock is set an hour off still excludes and is excluded |
| A consumer pass that meets a lease makes no wiring write but the request, and returns; it never waits | unit on `handle_wiring_changed` and the terminal handler, asserting the statements made |
| A request on a reservation with no state row inserts it | unit |
| A settle pass that meets a lease requests nothing | unit; Postgres-live with two settle steps |
| Paying is an update that only raises, to the s read under the lease | unit; Postgres-live with a request staged during the settle pass |
| A full pass stamps the deferred version; a nacked event at or below it is then a no-op | unit |
| A retry pass plans from rows re-read under the lease | unit: R1 and R3 as written by the review, failing on main and passing here |
| An active reservation's settle parks after five failures and a frozen one's does not | unit |
| The holder's success and release flips are unchanged | the existing `test_a_superseded_holders_failure_is_refused_but_its_success_is_recorded` and `test_a_superseded_l3_holder_records_only_its_success` stay green |

## Options rejected

| Option | Complexity | Why not, with the trace |
|---|---|---|
| Revision 1: the reconcile takes the row claim and skips held rows inside its pass | High | The review's eleven defects. To be sound it needs: a reservation lease anyway, for `ACTIVE` rows and keys with no row; a durable flag for an allocation whose release was deferred, honoured by the teardown and by any pass that pays; a content or version compare on the claim (R3); a stamp re-sized after the L3 residue read (the read moves behind the claim while PR #1490 sizes the stamp before it); a request at the first skip instead of the end of the pass; a ledger per reservation; a partial unique index so a key cannot have two non-`RELEASED` rows; and the database clock. With the reservation lease in place, all the row machinery buys is letting a reconcile work on a reservation's other rows while a retry pass drives one |
| The holder re-checks intent after its drive, with no settle pass | Medium | CONSERVATION stays open in two traces. The holder's fork read fails after its success flip (cabling answers 5xx): it cannot judge, the row is `ACTIVE`, nothing is owed. Or the holder dies between the flip and the re-check: the same end state |
| Issue option 1 (parks keep the stamp) plus a guard on the key-based failure write | Low | EXCLUSION stays open on an unrelated save: the build diff is desired minus `ACTIVE`, so a save that touches nothing near row X still puts X's still-desired key on the build side and the reconcile drives it beside the holder (effect 3). The owner rejected option 1 alone |
| Issue option 3: the reconcile waits for the holder | Low | The consumer handles one message at a time, so one held reservation stalls every reservation's events on that replica. The owner rejected it. Revision 1's in-process lock was the same stall and is withdrawn |
| Settle by delayed redelivery of the same event | Medium | Four redeliveries over 81 seconds, shared with real upstream failures; the fifth failure dead-letters a healthy event that is not staged again. It cannot serve a terminal event at all |
| Settle by a follow-up event through execution's outbox | Medium | The outbox has no delay; execution would publish on a subject reservations owns (RES-EVENT-2) or grow a subject and a filter; a follow-up at a stamped version is a no-op |
| Ask reservations to stage a heal | Medium | A new internal route and a cross-service round trip for state execution holds itself, and it cannot settle a frozen reservation |
| The settle step in the retry tick | Low | It needs an in-process guard against the consumer (the stall above, and a lock that is not reentrant when the settle pass calls the teardown), it runs only where `WIRING_RETRY_ENABLED` is true while requests are made wherever a consumer runs, and it settles up to a minute late instead of within seconds |
| One lease stamp sized for the whole pass | Low | A pass has no bound, so neither does the stamp: a holder that dies would block its reservation for the stamp's length, hours for a large L3 pin. Renewal per step bounds a dead holder to one term |
| `holder_record` again | High | Round 3: the holder decides from a row it cannot fully interpret |

The trade the lease makes against revision 1: a save that lands while any pass is
driving its reservation is applied whole by the settle step, seconds after that pass
ends, instead of being applied at once except for the held rows. In return there is one
mechanism, enforced at one choke point, with no partial pass to reason about.

## Spec rule edits for the implementing pull requests

`docs/specs/provisioning-and-wiring.md`; not edited by this record. Each phase changes
the rules it makes untrue in its own pull request; this is the union.

- **Header.** Add ADR 0018 to the Design records row.
- **Section 4.** The wiring state table gains the lease (take, renew, release) and the
  settle counters (request, pay) with their guards. Concurrency: "the two retry
  channels exclude each other with the claim stamp" becomes "every pass excludes every
  other pass of its reservation with the drive lease; the retry channels also carry the
  row claim until it is retired".
- **WIRE-CLAIM-1** (phase 1). Add: the update also requires the row's `intended` to
  equal the value the pass read.
- **WIRE-CLAIM-3**, as rewritten by PR #1490. Add that the row budget is what excludes
  two retry passes on one row, and that since the lease it is not what excludes a
  reconcile; removed in phase 4.
- **WIRE-CLAIM-4.** Proposed wording: "A retried row is claimed immediately before its
  own driver call (an L3 pin once, before its switch's login, with the budget of
  WIRE-CLAIM-3), never at selection. The reconcile, the terminal teardown, and the
  settle pass take no row claim: each holds its reservation's drive lease for the whole
  pass (WIRE-LEASE-1), and so does a retry pass, which claims its rows under it."
- **WIRE-CLAIM-8.** The limit about the reconcile becomes: "the reconcile, the
  teardown, and the settle pass write a reservation's rows only while they hold its
  lease, and no retry pass holds a row of that reservation meanwhile (WIRE-LEASE-1)".
  The limit about the holder's flips stays, as a decision of this record.
- **WIRE-CLAIM-9.** "or whose claim a writer that does not claim cleared" is removed
  once phase 3 has shipped for all three layers.
- **New section 8.16, WIRE-LEASE-1 to WIRE-LEASE-6.** Who takes the lease; take, renew,
  release; the term; where a pass renews and the refusal with no lease in context; what
  a pass does when it loses it; the database clock.
- **New section 8.17, WIRE-SETTLE-1 to WIRE-SETTLE-7.** The request; where the settle
  step runs; selection; what it runs by reservation state; paying and the stamp;
  spacing and parking; the alarms.
- **WIRE-CONSUME-2.** One settle pass at most between fetches.
- **WIRE-ORDER-3, WIRE-ORDER-4, WIRE-ORDER-10.** A `wiring_changed` that meets the
  lease requests a settle and is not stamped; a full pass stamps the deferred version.
- **WIRE-DISPATCH-3, WIRE-TEARDOWN-1 to WIRE-TEARDOWN-3.** A terminal event that meets
  the lease freezes, requests a settle, and leaves the ledger teardown to the settle
  pass.
- **WIRE-RETRY-5, WIRE-RETRY-6, WIRE-RETRY-11, WIRE-RETRY-13, WIRE-RETRY-14,
  WIRE-RETRY-16, WIRE-RETRY-19.** A retry pass takes the lease per reservation and
  re-reads its rows under it; `in_progress` also means the reservation is leased or
  owes a settle; the selects' third exclusion; the tick line's new counters.
- **Section 13.** Remove the two #1481 limits; add the limits under "Where it does not
  converge".

Also `CHANGELOG.md`, `docs/specs/CONVENTIONS.md` (the lease as the one helper), the
module docstring of `wiring_claim.py`, the docstrings of the three applies, and the
docstring of `record_route_reconciled` (its multi-process case becomes unreachable).

## Test plan

At HERD's five levels.

1. **Unit** (SQLite, `services/execution/tests/`). The invariants table. Phase 1: the
   claim per `intended` state, and the read-back answer `still_failed` for a row that
   is `FAILED` and unheld. R1, R2, and R3 enter the suite as written by the review:
   each fails on main and passes at its phase (R1 at phase 1, R2 and R3 at phase 3).
   The allocation trace under freeze. The upsert on a missing state row, for the take
   and for the request. A pass that raises after taking the lease releases it and the
   message is nacked as today; a pass that meets the lease has made its request before
   anything that can raise. Tests that pin main and change on purpose: at phase 3, the
   three `test_after_the_reconcile_clears_the_claim_*` tests (a reconcile can no longer
   run beside the holder); at phase 4, the row-claim tests go with the code.
2. **Functional** (the service's real surface, in process). The internal retry route
   answers `in_progress` for a reservation whose lease a reconcile holds, and for one
   that owes a settle. `handle_reservation_event` with a real payload for a deferred
   save and a deferred terminal event. The consumer stall: with a lease held by another
   pass, the handler returns after one statement, and a second reservation's event is
   handled without delay.
3. **Postgres-live** (the `_gated_driver_stub` and `_loser_inside_holders_drive`
   pattern of `test_wiring_retry_claim_race_live_pg.py`; every overlap staged with
   events, every wait bounded, no sleep):
   - a save lands while a retry pass is inside its drive, once each for an L1 connect,
     an L2 join, and an L3 provision: one driver call for the row, a request recorded,
     the version not stamped;
   - the holder then succeeds, and separately fails: the settle pass releases the row;
     the version is stamped only then;
   - the lease expires while the save is deferred (the test moves the expiry into the
     past; it does not wait): the settle pass takes it;
   - two saves in a row are both deferred: one settle pass pays both and stamps the
     larger version;
   - a request arrives during a settle pass: the mark stays owed;
   - two settle steps on two connections for one reservation: one runs, the other
     writes nothing;
   - a settle step against a terminal teardown, both orders: one ledger teardown runs;
   - a terminal event lands while a retry pass is inside an L3 provision, with both
     holder outcomes;
   - a drive longer than the lease term keeps the lease by renewing (a virtual clock,
     as PR #1490's tests use), and a holder that stops renewing is replaced after one
     term, at which point its next renewal fails and it makes no further call;
   - an L3 provision whose residue is read under the lease: the residue it drives is
     the residue at lease time.
   The two live tests that pin main,
   `test_teardown_during_a_held_l3_provision_leaves_its_routes_recorded_for_removal` and
   `test_a_reconcile_failure_write_during_a_held_l1_connect_still_records_it`, are
   rewritten at phase 3. Each fixed test carries its mutation proof: the take made
   unconditional, the renewal check removed, the request moved after the handler's
   first read, the pay made unconditional, the re-read under the lease removed.
4. **Integration** (running stack, mock drivers). The L1, L2, L3, and ledger teardown
   suites stay green; no API changes. One convergence test with the mock drivers'
   `HERD_mock_sleep_ms` knob holding a retry open while a save lands, asserting the end
   state by polling the wiring status, never by timing. It is the only level that
   proves the settle step runs in a real consumer loop.
5. **Load** and **browser end to end**: no new suites. No new route and no UI change:
   the retry summary already counts `in_progress` and words it "already retrying"
   (`frontend/src/api/reservations.ts`). The settle step's cost when nothing is owed is
   one indexed select per fetch, pinned by a unit test that counts statements, not by a
   wall-clock bound. `make test-load` and the nightly e2e run unchanged and must stay
   green; the load suite's concurrent saves now exercise deferral, and the deferral
   count is asserted to be reported, not to be zero.

## Delivery phases

Each phase is one pull request (phase 3 is three), mergeable alone, and leaves main
correct.

1. **The row claim reads `intended`.** `WiringRowClaims.claim` and its three call
   sites, with the read-back change. The in-process `held` shortcut is unchanged: a
   pass claims a row once. Closes R1.
2. **The lease, the mark, and the settle step, for the consumer's passes.** The
   migration; take, renew, release; the context variable and the renewal points; the
   reconcile body factored into one function; deferral in `handle_wiring_changed` and
   the terminal handler; the settle step in the consumer loop with its stamp, spacing,
   and parking; the tick line's counters and the overdue warning. Retry passes are not
   touched: against a reconcile they behave as on main.
3. **Retry passes take the lease and plan under it**, one layer per pull request (L1,
   L2, L3): the take before the re-read of the reservation's rows, held through the
   apply; the selects' new exclusion; the manual retry's `in_progress`. Closes #1481,
   R2, and R3 for that layer. The refusal of a wiring driver call with no lease in
   context is switched on with the last layer, when every caller has one.
4. **The row claim is retired.** `claimed_until`, `WiringRowClaims`,
   `claim_write_clauses`, `write_guard`, `holder_or_unclaimed`, and the L3 row budget
   go. A separate decision; see open question 4.

Phase 2 must precede phase 3. Phase 1 is independent of the others and can ship today.
PR #1490 is independent of all of them. In a deployment with one consumer replica,
phase 2 defers nothing in normal running (the consumer is serial, and retry passes do
not hold the lease yet); the one case is a redelivery that meets the lease of a pass
that died, which is then settled after one lease term.

## Consequences

- A save, or a reservation's end, that lands while any pass is driving that
  reservation is applied by the settle step after that pass ends, not at once. Idle,
  that is within seconds.
- A manual retry answers `in_progress` for a whole reservation that is leased or owes a
  settle, not for single rows.
- One migration on `reservation_wiring_state`. No new setting, route, event, or
  subject.
- The consumer loop gains a step between fetches, and the reconcile body gains a second
  caller on the same task.
- Every driver attempt and upstream read costs one short update on the lease.
- A reservation whose settle keeps failing is parked after five attempts and needs a
  save or an operator, as a dead-lettered event does today.
- Tests that pin main's behaviour change deliberately; the test plan lists them.

## Open questions for the owner

Each has this record's recommendation.

1. **Granularity.** The claim is per reservation, not per row, which changes the shape
   the owner chose. Recommendation: accept. The row-granular form needs a reservation
   lease anyway and seven further mechanisms (Options rejected, first row), and its
   only gain is applying the rest of a save while one row is being retried.
2. **Parking.** After five failed settle attempts on an active reservation: park
   (recommended, the `max_deliver` analogue) or keep trying at the capped spacing,
   which re-drives failing rows with no attempts cap for as long as the failure lasts.
3. **The stamp.** Cabling's internal fork answer carries the fork's versions
   (`ForkDetailResponse.versions`). If the latest version row and the connections it
   returns are committed together by a save (`fork_save_service.py`; not verified
   here), every full reconcile could stamp the fork's own version and close the
   rebuilt-wire gap named under D6, which exists on main for gap and heal reconciles.
   Recommendation: verify it, and if it holds, do it in phase 2; otherwise stamp the
   deferred version and keep the limit.
4. **Phase 4.** Retiring the row claim deletes the #817, #1479, and #1482 machinery and
   makes a dead holder cost two minutes instead of the row budget. Recommendation: yes,
   after one release has run with phase 3. Issue #1493's limit is still worth having
   for the consumer's sake: a drive of thousands of routes is hours of work whoever
   holds what.
5. **The tick's lease scope.** Per reservation (recommended): the lease is held only
   while that reservation's rows are driven. Or once for the tick's whole batch, which
   is a smaller code change and defers saves for every reservation in the batch for the
   batch's whole length.
6. **Deferral latency.** A save is delayed whenever anything is driving its
   reservation, where revision 1 delayed only the held rows. Recommendation: accept,
   and watch the deferral count the tick line reports; if it matters, the holder can
   run the settle pass itself before it releases, as a later change.
7. **Should a build pass stop when a renewal shows its reservation frozen?** It would
   save driver calls whose results the freeze branch then parks. Recommendation: not
   now; it is not needed for correctness.
8. **Issue #1413.** The interleaving it describes needs two passes of one reservation
   at once, which the lease excludes after phase 3. Recommendation: have that issue's
   owner confirm it against this record before closing it.
9. **The lease term's allowance.** Two minutes at the defaults is this record's
   proposal, not a measurement. Recommendation: decide it in phase 2's review with the
   virtual-clock tests in hand.

## Revisit when

- a step that a pass makes can outlast the lease term by design (a driver action with
  no timeout, an upstream read with a longer one);
- the consumer handles more than one message at a time (the settle step then needs its
  own place);
- wiring gains a writer that is not one of the four entry points;
- deferrals become common enough to notice (open question 6);
- two reservations driving one switch at once becomes a problem: the lease is per
  reservation and says nothing about it.

## Revision history

- Revision 1, commit `6a6b912e`: row-granular claim for the reconcile, a settle mark,
  and a settle step in the retry tick.
- Revision 2: reservation drive lease for every pass, the settle step in the consumer
  loop, the plan made under the lease. Revision 1's decisions D1 to D10 are withdrawn;
  its reading of main, which the review confirmed, is kept.
