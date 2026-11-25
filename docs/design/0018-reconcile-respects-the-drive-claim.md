# Decision: The Wiring Reconcile Respects the Drive Claim, Issue #1481

Status: Proposed 2026-11-20, revision 3, after two adversarial reviews (of revision 1,
commit `6a6b912e`, and of revision 2, commit `9f443621`). The owner chose option 2 of
issue #1481: the reconcile takes a drive claim, skips what a retry pass holds instead of
waiting, and what it skipped is settled later. Since revision 2 the claim is one drive
lease per reservation, not the row claim; that change of granularity has not been ruled
on by the owner and is open question 1. Deciders: the owner. No code in this document.
Context verified by reading the HERD-public tree at `821eed1c` on 2026-11-20 (PR #1490
is merged); symbols are the stable reference and line numbers are as of that commit.
Four behaviours of main (R1 to R4) were reproduced at `821eed1c` during the reviews,
with SQLite tests kept outside the repository. Every other trace is a reading of the
code, not an execution, and the test plan names what has to prove it. Three designs for
this defect have been built and reverted, and two revisions of this record were found
unsound in review, so each trace says which kind it is, and the first section says
plainly what stopping early costs.

## What has shipped since this record was written

The Context, the traces, and the line citations below read main at `821eed1c`, the
commit this record names, and the items in this list have merged since; where the body
and this list disagree, this list and the specifications are current. Each item was
checked against the merged code or the specification on main, not against this record.

- PR #1500 (issue #1496) shipped phase 1: a retry claim now also requires the row's
  `intended` to equal the direction its call site drives (WIRE-CLAIM-1,
  `direction_admits`). This closes R1, so "R1, reproduced" in "What the code shows that
  the issue does not" describes main before this change, and the phase 1 row of
  "Smallest safe first step" is done.
- PR #1500 also admits a release claim on a frozen reservation whatever the row's
  `intended`, and never admits a build there; the phase 1 wording in "Spec rule edits
  for the implementing pull requests" omits that exception.
- PR #1515 (issue #1498) moved the schema extraction off the event loop
  (`load_driver` awaits it through `asyncio.to_thread`) and put a total deadline on the
  driver download (`DRIVER_DOWNLOAD_DEADLINE_SECONDS`, 120 seconds). These are two of
  the bounds in phase 2 part (b), in D2, and in item 9 of "What the code shows that
  the issue does not". The rest of phase 2 part (b) was not rechecked.
- PR #1514 (issue #1493) set the per-switch route limit this record called proposed:
  `herd_common.l3_route_limit.MAX_ROUTES_PER_SWITCH` is 1,000. The 25 hour figure in
  D13 is therefore computed against a limit that exists, not a proposed one.
- PR #1516 (part of issue #1501) made the terminal teardown park each FAILED
  intended-`ACTIVE` L1 and L2 build, as it already parked L3 pins, and remove it with
  the ACTIVE rows (WIRE-TEARDOWN-2 and WIRE-TEARDOWN-6). The Teardown row of "What the
  reconcile does to a FAILED row today" lists L3 pins only.
- PR #1516 also means a teardown no longer drives only `ACTIVE` rows: it drives the
  parked FAILED builds as well. The sentence in D8 about a frozen reservation's settle
  is true of rows that fail again, but its premise is out of date.
- PR #1516 changes WIRE-TEARDOWN-6 as "Where it does not converge, or converges only
  with a condition" quotes it: a failed L1 or L2 build on a frozen reservation is now
  parked and removed by the teardown. What stays as it was is an L1 or L2 row held by
  another retry pass's live claim at the teardown, which the specification lists as
  left alone.
- PR #1516 also rewrote WIRE-TEARDOWN-2 and WIRE-TEARDOWN-6 and added WIRE-LEDGER-17,
  so the rule texts this record says it will amend must be reread in the
  specifications before any phase is built from "Spec rule edits for the implementing
  pull requests".

Line-number citations in the body are as of `821eed1c`.

## Smallest safe first step

The owner may stop after any phase. Sizes are pull requests and rough scope: small is a
file or two plus tests, medium is a lane of ten to twenty-five files, large is more.

| Stop after | Pull requests so far | What that phase ships | Closes | Stays open |
|---|---|---|---|---|
| Phase 1 | 1, small | The existing row claim also requires the row's `intended` to equal the direction the call site drives. No schema, no new plumbing | R1: a retry pass build-drives a row another pass parked | The three effects of #1481; R2; R3; overlapping reconciles across replicas; the allocation leak |
| Phase 2 | 3 (two more, small to medium) | Two repairs to main that stand alone: the reservation-wide allocation release check, and bounded steps in a pass | A VLAN allocation left `ACTIVE` with no members when an L2 apply raises midway (reachable on main today); an event loop blocked for up to 10 seconds by a driver load; a download and a sandbox reap with no total bound | The same races as after phase 1 |
| Phases 3 to 5 | 7 (two medium, one large, one medium with frontend) | The lease, fenced writes, the settle mark and step, for the consumer's passes; the owed and parked states shown to the owner | Two reconciles, or a reconcile and a teardown, driving one reservation at once from two replicas | The three effects of #1481; R2; R3: retry passes still run outside the lease |
| Phase 6 | 10 (three more, medium, one per layer) | Retry passes take the lease and plan under it | #1481's three effects, R2, R3 | Only the limits under "Where it does not converge" |

Phase 1 was confirmed safe alone by both reviews, at `821eed1c`. It closes R1 and
nothing else. Phase 2 is worth shipping whatever is decided about the rest: both halves
repair main as it is today.

Whether to go past phase 2 is a real question, and this record does not pretend it is
easy. For: R2 and R3 are reproduced, they leave a wire or a route set on a switch that
no pass will ever correct, and nothing smaller than a claim plus a settle closes them
(Options rejected has the traces). Against: the window is a save or a reservation's end
landing while a retry is driving the same reservation, which is rare; phases 3 to 6 are
seven pull requests of concurrency code in the area that has already been wrong five
times; and the design still has limits. If the owner stops after phase 2, #1481 becomes
a recorded limit in the specification with R2 and R3 as its named cases. That is a
defensible outcome, and this record's recommendation is to decide phases 3 to 6 as one
arc or not at all: phases 3 to 5 alone add machinery and close only a cross-replica
case.

## Context

### The claim, and who honours it today

A wiring ledger row (`l1_connection_assignments`, `l2_port_assignments`,
`route_assignments`) carries `claimed_until`. A retry pass takes the claim with one
compare-and-swap immediately before the row's own driver call
(`WiringRowClaims.claim` in `services/execution/app/services/wiring_claim.py`): the
update matches the row id, status `FAILED`, and a stamp that is null or in the past,
and writes now plus a budget: 5 minutes for an L1 or L2 row at the defaults, and since
PR #1490 `l3_pin_claim_budget` of the pin's drive sequence for an L3 pin (19 minutes
for 10 route operations). Issue #817 added it so the retry tick and the manual retry
never drive one row at once. Issue #1479 (PR #1486) made every failure write and every
settle a retry pass makes respect it (`claim_write_clauses`, `write_guard`), judged by
the stamp in the database (WIRE-CLAIM-8, WIRE-CLAIM-9).

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
  token is on it.
- A row's **key** is (switch, canonical port pair) for L1, (switch, port) for L2, and
  (switch) for L3, always within one reservation.
- **Deferred**, **interrupted**, and **failed** are the three ways a pass ends without
  completing; D5 defines them.

### What the reconcile does to a FAILED row today

| Layer | Where a reconcile or teardown meets a FAILED row | Function (line at `821eed1c`) | Checks the claim |
|---|---|---|---|
| L1 | stale-build park, full reconcile only | `handle_wiring_changed` calls `park_stale_l1_build` (`nats_consumer.py:5013`); the park clears the stamp (`l1_assignment_service.py:907`) | no |
| L1 | release drive of the parked pair; build drive over a FAILED row | `_apply_wiring_pairs` (`:2242`), `_apply_one_port_action` (`:2544`); the claim at `:2592` runs only when a ledger is passed, and the calls at `:5022` and `:5041` pass none | no |
| L1 | load failure, login failure, op failure, unresolvable hop | key branch of `record_l1_failed` (`l1_assignment_service.py:736`; stamp cleared at `:855`) | no |
| L2 | stale-build park; direct release of a stale row with a nil or unknown allocation | `_reconcile_l2_memberships` calls `park_stale_l2_build` (`:3825`) and `release_l2_membership` (`:3814`) | no |
| L2 | drives and failure writes | `_apply_l2_memberships` (`:3246`), `_apply_one_vlan_action` (`:3561`), key branch of `record_l2_failed` (`l2_membership_service.py:401`, `:496`) | no |
| L3 | stale-build park | `_reconcile_l3_adjacency` calls `park_stale_route_build` (`:4811`); the park clears the stamp (`route_service.py:773`) | no |
| L3 | drive-gate refusal write | `record_route_failed` by key (`:4773`) | no |
| L3 | release of an unapplied pin | `release_unapplied_route_pin` (`route_service.py:890`) | yes: its select carries `unclaimed`, and a held row is left alone |
| L3 | provision over a FAILED pin, deprovision, failure writes | `_apply_l3_adjacency` (`:4224`; claim at `:4496`), key branch of `record_route_failed` (`route_service.py:584`, `:680`) | no |
| Teardown | park of each FAILED intended-`ACTIVE` pin, release of a pin with no routes, deprovision | `_teardown_from_ledgers` (`:5075`; park `:5183`, release `:5187`, apply `:5191`) | no |

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
   and pins r0 `ACTIVE`. No retry channel reads `ACTIVE` rows.
7. **R4, reproduced.** With last applied 4, an event at version 6 that carries a delta
   takes the full-reconcile path and stamps 6. D8 relies on this.
8. An L2 apply that raises between two switches (`_apply_l2_memberships`: a membership
   removed on one switch, then a switch or template read that raises
   `TransientUpstreamError` for the next) never reaches `_release_orphaned_allocations`.
   The redelivery no longer sees the removed membership, so the allocation is not
   among the ones its removes touch, and it stays `ACTIVE` with no members. Read, not
   reproduced; it is on main today and needs no concurrency.
9. `load_driver` (`services/execution/app/services/driver_loader.py`) runs
   `extract_config_schema_json` on the event loop: a synchronous `subprocess.run` of up
   to `status_check_timeout_seconds` (10) that blocks every coroutine in the process.
   `download_driver_package` uses its own httpx client with a 30 second per-phase
   timeout and no total. The sandbox's `subprocess.run` kills the child on timeout but
   not its process group.

R2 and R3 show that the defect is wider than "the reconcile ignores the claim": a pass
acts on a plan that another pass has since made untrue.

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
  documents a poller fleet (`EXECUTION_POLLER_ONLY`). So two passes of one reservation
  can run in different processes today, and anything exclusive has to live in the
  database.
- Every wiring driver call goes through `_run_driver_with_retry`, and the ledger writers
  are reached only from four entry points: `handle_wiring_changed`,
  `_teardown_from_ledgers`, `run_wiring_retry_tick`, and `reattempt_reservation`.
- `reservation_wiring_state` may have no row for a reservation (`get_wiring_state`
  returns None). `stamp_last_applied` and `freeze_reservation_wiring` insert it by
  read, insert, and a fallback on the integrity error, an idiom that cannot report
  whether a conditional write matched.
- The terminal handler (`handle_reservation_event`) freezes, then runs
  `_teardown_from_ledgers`, then `_execute_dynamic_teardown`, which needs the event's
  `user_id` and deletes each materialized device from inventory. Nothing read here
  forbids a dynamic template whose device is a switch, so a ledger row may name a
  device that the dynamic teardown deletes.

### Five designs that failed

| Design | What it did | What broke |
|---|---|---|
| #1479 round 1 | Added the claim condition to the retry failure writes by row id only | The guarantee was narrower than stated: other retry-pass writes, and a holder whose claim had expired and been retaken, still wrote (commit `f5a98551`) |
| #1479 round 2 | Put the holder's success and release flips behind the stamp too | A writer that never claims (the teardown park) cleared the stamp during a held L3 provision; the holder's success was refused and the configured routes stayed on the switch with no ledger row (commit `fd9ccbf2`) |
| #1479 round 3, `holder_record` (as of commit `a8b14242`) | The holder decided from the row as it found it: PARK or REOPEN | PARK wrote by key. When held row X had been released during the drive and the key rebuilt as a new row Y, PARK flipped Y toward release, and the release channel would tear down a wire the fork intends (commit `b8e728b2`) |
| This record, revision 1 (commit `6a6b912e`) | The reconcile took the row claim, skipped held rows inside its pass, and owed a settle paid by a step in the retry tick | A row that is `ACTIVE`, or a key with no row, cannot be claimed, so two settle passes on two replicas both drove it; a pass that skips part of a reservation leaked an allocation, could loop, and requested its settle too late; the claim compared direction but not content (R3); an in-process lock stalled the consumer |
| This record, revision 2 (commit `9f443621`) | One lease per reservation, renewed before each step; ledger writes checked the lease first and success flips did not check it at all | The check and the write were two statements, so a holder that stalled between them wrote under the next holder's lease; "no pass is partial" was false for a pass that raises midway; the settle select starved behind one held lease; a lost lease on L3 stranded routes; the terminal teardown and the dynamic teardown changed order |

`a8b14242` is in main's history: the #1486 branch was merged by a merge commit, and
`b8e728b2` removed `holder_record` later on the same branch.

### The two invariants

- **EXCLUSION.** (E1) At most one pass has a wiring driver call in flight for one
  reservation. (E2) A ledger write for a reservation's rows commits only while its
  writer holds that reservation's lease, decided inside the write's own transaction.
  One exception is kept from main: the two cross-reservation writers still flip another
  reservation's `ACTIVE` row (WIRE-LEDGER-11, WIRE-LEDGER-12).
- **CONSERVATION.** No intended change is dropped, and no change made on a device goes
  unrecorded. (C1) Every `wiring_changed` and terminal event that reaches its handler
  is applied by the pass that received it, or leaves an owed settle in the database
  before the message is acked. (C2) A driver call whose outcome its pass could not
  write to the ledger leaves an uncertain record, which the next full pass folds into
  the ledger before it plans. (C3) An owed settle is paid only by a full pass that held
  the lease and completed, or is parked where the owner can see it and apply it.

E1 holds while every step a pass makes ends inside the lease term (D2). E2, C1, C2, and
C3 do not depend on timing.

## Decision

### D1. One drive lease per reservation, and every pass holds it

`reservation_wiring_state` gains a lease: a token and an expiry. A pass takes the
reservation's lease before it reads anything it plans from, holds it until its last
write, and releases it. Every kind of pass takes it: the `wiring_changed` apply, the
terminal teardown, the settle pass, the retry tick for each reservation it works on,
and the manual retry.

The reservation is the smallest unit that can be claimed for everything a pass does.
A row claim cannot cover a release of an `ACTIVE` row or a build of a key that has no
row yet, and those are the drives two reconciles collide on.

Inside the lease the reconcile path stays as it is on main: no row claim, no key-to-row
map. The keyed writers assume one non-`RELEASED` row per key, which no index enforces
for `FAILED` rows; under the lease two passes cannot insert a row for one key of one
reservation at once, so the assumption becomes a consequence, with no schema change.

### D2. The lease: taking, renewing, the term

- **Take.** One statement: `INSERT ... ON CONFLICT (reservation_id) DO UPDATE ... WHERE`
  the stored expiry is null or not later than `clock_timestamp()`, writing the pass's
  fresh token and an expiry one term ahead. One row written means taken. It must be
  this single statement: the read, insert, and fallback idiom of `stamp_last_applied`
  cannot say whether the condition matched. It runs with a short `lock_timeout`; a
  timeout reads as "held".
- **Renew.** One update that moves the expiry one term ahead where the token is still
  the pass's own, returning the freeze flag. It does not test the expiry: an expired
  lease nobody took is still the pass's. No row written means the lease was lost.
- **Release.** One update that clears the expiry where the token is the pass's own, in
  a `finally`, which also runs on cancellation.
- **No in-process belief.** A lease object is made per pass per reservation with a
  fresh token. Taken, renewed, and lost are each decided by a statement's row count,
  unlike `WiringRowClaims.claim`, which answers True from its `held` map.
- **The clock.** The expiry is written and compared by the database, in the statement,
  with `clock_timestamp()` and not `now()` (which is the transaction's start and can be
  stale). No process compares a stamp with its own wall clock, so any wall-clock offset
  between replicas is tolerated.
- **Where a pass renews.** Before every driver attempt, before every upstream read, and
  with every commit (D3). The lease travels in a context variable, so no apply
  signature changes; the consumer's task, the tick's task, and a request's task each
  have their own context. `_run_driver_with_retry` refuses a wiring driver call with no
  lease in context, once every caller has one.
- **Every step between two renewals is bounded.** Revision 2 checked a gap before
  handing a driver attempt to a thread and left the awaits around it unbounded. Now the
  work carries its own bound:
  - the gap check runs inside the worker thread, immediately before `subprocess.run`,
    so time spent queueing for the thread pool is counted; an attempt that finds its
    renewal too old does not start, and the pass renews and offers it again;
  - `download_driver_package` and every internal read get a total deadline, not only a
    per-phase timeout;
  - `extract_config_schema_json` moves off the event loop;
  - the sandbox kills the child's process group on timeout, so a grandchild holding the
    pipes cannot keep the reap waiting;
  - the pass's database sessions set `lock_timeout`.
- **The term.** `claim_budget()`, the one-row budget (three driver actions' worst case:
  5 minutes at the defaults), plus 60 seconds when `ALLOW_DRIVER_PIP_INSTALL` is on,
  and never less than twice the longest bounded step. One step is at most one driver
  attempt or one bounded await, so at the defaults the slack is about four and a half
  minutes. The term follows the same setting the steps do, so the ratio holds at any
  `EXECUTION_TIMEOUT_SECONDS`; the floor covers the small values, where a download is
  longer than a driver attempt. With every step bounded below the term, a step can
  outlast it only if the process itself is paused. D3 and D6 say what happens then.

A pass of any length keeps the lease by renewing. A holder that dies frees its
reservation within one term, whatever it was driving.

### D3. Every ledger write is fenced by the lease, in its own transaction

Revision 2 renewed and then wrote, in two statements. That is check-then-act: a holder
that stalls between them writes under the next holder's lease.

Now the renewal is part of the write. Every transaction a pass commits runs the renewal
update inside it, before the commit, and commits only if that update wrote a row. The
renewal locks the lease row until the commit, and a take is a write of that same row,
so the two are ordered:

- the take commits first: the fenced transaction's renewal writes no row, and the whole
  transaction is rolled back, its ledger write with it;
- the fenced transaction commits first: the take then finds an expiry in the future and
  does not match.

So no ledger write of a superseded holder is ever visible to the pass that superseded
it, and no pass plans from a row that another pass is still writing. This holds for
every write: failure writes, parks, releases, allocation writes, and the success and
release flips, which revision 2 left unconditional. The parks (`park_stale_l1_build`
and its siblings) are a select and then an update by primary key, not a
compare-and-swap; they stay as they are, because under the fence no other pass's write
can land between the two.

The fence lives in one place: the session factory a pass is given after it takes the
lease. Every session it makes fences every commit, so a writer cannot forget it. That
is the answer to round 1, where writers were guarded one by one and some were missed.

A refused write is not the end of the matter when it recorded a driver call. D6 says
what the pass does next.

### D4. A pass plans under the lease

Every read a pass plans from is made after it took the lease: the wiring state row
(frozen, last applied version), the fork's intended set, and the ledger rows. A retry
pass re-reads the rows it selected, by id, after it takes their reservation's lease; a
row that is no longer `FAILED` is dropped, and the pass uses the `intended`, `routes`,
and `vlan_assignment_id` it re-read, then validates build intent against the fork as it
does today.

This closes R1 and R3: nothing a pass decided before the lease is used after it.

### D5. Three ways a pass ends without completing

**Deferred: it met the lease before any wiring work.** Nobody waits.

| Pass | When another pass holds the lease |
|---|---|
| `wiring_changed` apply | After the frozen check and the stale-version check (both read values that only move one way): requests a settle carrying its fork version (D7), does not stamp, returns. The message is acked |
| Terminal teardown | Freezes first, as today (WIRE-TEARDOWN-1), then requests a settle carrying the event's `user_id`, and runs neither the ledger teardown nor the dynamic-instance teardown (D9). The message is acked |
| Settle pass | Passes over the reservation and stamps the attempt time (D8). Requests nothing |
| Retry tick | Passes over the reservation's rows: they stay `FAILED`, no attempt is counted, and they count `in_progress`. Its three selects leave out rows of a reservation whose lease is live |
| Manual retry | After the `not_retryable` and `frozen` classification (WIRE-RETRY-11), reports the reservation's other rows `in_progress` |

Only a consumer pass requests a settle, once, before any wiring work (the teardown
after its freeze). A settle pass that meets a lease cannot request another, so the loop
the first review found is impossible.

**Interrupted: it held the lease, did some work, and must stop.** Two causes:

- a renewal or a fenced commit wrote no row: the lease was lost;
- a renewal reported the reservation frozen, and it was not frozen when the pass took
  the lease. A pass that is not a teardown ends there, so a reservation's end is not
  kept waiting by a pass that is still building (D9).

An interrupted pass makes no further wiring driver call and no further ledger write. It
closes the open execution run row as failed with a fixed reason, through a plain
session (that row is an audit record, not a ledger row), and logs out of the switch
session it had open: a logout changes no wiring, so it is the one driver call exempt
from the lease check, and the applies run it from a `finally` around each switch. If
the write that was refused recorded a driver call, the pass writes an uncertain record
(D6). A consumer pass then requests a settle, because the rest of its work has to be
done by a full pass, is not stamped, and is acked. A retry pass requests a settle only
when it wrote an uncertain record; otherwise its rows are still `FAILED` and the tick
selects them again. A settle pass leaves its mark owed and counts no failure.

An interrupted pass is not a deferred one: it has done wiring work, and what it did is
on the ledger (its fenced commits) or in an uncertain record.

**Failed: it raised.** An upstream or permanent error, as today: the lease is released,
and the consumer nacks or dead-letters the message exactly as on main. What the pass
committed before it raised stays, as on main, and the redelivery repeats it.

The retry channels also pass over a reservation that owes a settle whose attempts have
not failed (D8), for at most ten minutes. The settle pass does a full reconcile, which
rebuilds and releases everything a retry would.

### D6. A driver call whose record was refused: the uncertain record and the fold

Round 2 refused the holder's success write and walked away, and the routes it had
configured were recorded nowhere. Revision 2 avoided that by never refusing a success
write, which is what let a superseded holder write under another pass's lease. Neither
is acceptable, so a refused write gets somewhere else to go.

**The record.** A small table, `wiring_uncertain_keys`: reservation, layer, the key,
and for L2 the allocation, for L3 the routes. When a fenced write that records the
outcome of a driver call is refused, the pass inserts one row for that key, through a
plain session, together with a settle request. For L3 the routes are what the
partial-failure write would have stored: everything the drive tried to configure plus
the residue whose removal did not confirm. A refused failure write counts too, because
a call that timed out may still have landed. A refused park or a release with no driver
call leaves no record: nothing happened on the device.

The record is append-only and is not a ledger row, so it needs no lease and no later
ledger write can erase it.

**The fold.** Every full pass (an event's full reconcile, a settle pass, a teardown)
starts by folding the reservation's uncertain records into the ledger, under its lease,
each in one fenced transaction that also deletes the record:

- L1 and L2: the key's non-`RELEASED` row is set `FAILED` with attempts 0 and a fixed
  retryable reason, or such a row is inserted; an L2 row takes the recorded allocation,
  so its removal has a VLAN number to name;
- L3: the pin's stored routes become their union with the recorded routes, and the pin
  is set `FAILED`, or such a pin is inserted;
- the direction: intended `ACTIVE` on a reservation that is not frozen, intended
  `RELEASED` on a frozen one.

Then the pass plans as usual, and the machinery that exists settles the key in either
case. Not frozen: a `FAILED` intended-`ACTIVE` row whose key the fork intends is on the
build side and is driven again; one whose key the fork does not intend is a stale
build, which the reconcile parks and releases. Frozen: a `FAILED` intended-`RELEASED`
row is the release-direction retry's. An L3 provision over the folded pin first removes
the stored routes its route set does not name (WIRE-L3-19), which is what takes a
superseded holder's routes off the switch.

An event that would have applied a carried delta takes the full-reconcile path when its
reservation has an uncertain record.

The fold moves an `ACTIVE` row to `FAILED`. Round 3's REOPEN did the same and was
wrong; the differences are that the fold is made by the pass that holds the lease, from
a durable record, before it plans, and that on a live reservation it never points a row
toward release: it only says "drive this key again and see", and the fork read under
the lease decides the direction.

It relies on a driver accepting a repeated connect, join, or route call, and a removal
of something already absent. That is the same reliance a crash between a driver call
and its record has today, and the retry of a row whose call timed out.

### D7. The settle mark

`reservation_wiring_state` gains a request counter, a paid counter, the largest fork
version among deferred events, the `user_id` of a deferred terminal event, a failure
count, and the time of the last attempt (recommended names: `settle_requested_seq`,
`settle_paid_seq`, `settle_fork_version`, `settle_user_id`, `settle_failures`,
`settle_attempted_at`). A settle is owed while requested is above paid. A partial index
on that predicate keeps the settle step's select to the owed rows.

A request is one upsert statement (the state row may not exist) that raises the request
counter and the deferred version, committed before the handler returns.

### D8. The settle step

**Where it runs.** In the consumer's own loop (`_consumer_loop`), after each fetch
whatever its outcome: at most one settle pass between fetches. So the consumer never
waits for a settle pass, because it is the one running it; a settle step runs in every
process that can make a request; and when idle it runs within
`NATS_FETCH_TIMEOUT_SECONDS` (5 seconds) of the request.

**Selection.** One reservation that owes a settle, is not parked, whose spacing has
elapsed, and whose lease is not live, least recently attempted first, with `FOR UPDATE
SKIP LOCKED`. The select's transaction commits before anything else happens, so its
row lock is never held across a pass (it would block every upsert, freeze, and stamp on
that row). Then the take. A take that loses the race stamps the attempt time and moves
on. Excluding live leases in the select and stamping a lost take are both needed: the
first keeps one long-held reservation from being selected by every replica after every
fetch while the others wait, and the second covers the race the first cannot see.

**The pass.** Under the lease it reads the request counter as s, the deferred version
as m, and the freeze, folds (D6), then:

1. frozen: runs the teardown of D9;
2. not frozen: runs the full three-layer reconcile from cabling's current intended set,
   the body of `handle_wiring_changed`'s full-reconcile branch with the L2 and L3
   passes, factored into one function both callers use.

**Paying.** When the pass completes it stamps the last applied version, sets paid to s
by an update that only raises it, and clears the failure count. A request made during
the pass is above s and stays owed. An event-driven full reconcile or teardown that
completes under the lease pays the same way. A carried-delta apply pays nothing.

**The version.** A deferred event is not stamped, so a later event above it is not
contiguous and takes the full-reconcile path (WIRE-ORDER-4; R4 reproduces it). A full
pass that completes stamps the largest version it can prove it applied, through
`stamp_last_applied`, which never lowers: m, and its own version for an event, because
the fork was read after every deferred event up to m was staged. A nacked event at or
below m then arrives stale and is a no-op (WIRE-ORDER-3).

One gap remains, and it is main's today: a full reconcile reads the fork as it is now,
which can be above the version it stamps. An event between the stamp and the fork's
version then arrives contiguous and its carried delta is applied over newer state; if a
later version removed a wire that delta built, the wire is rebuilt until the later
event lands. Stamping the fork's own version would close it; open question 4.

**When the pass raises.** The failure count goes up, the lease is released, and the
mark stays owed. Attempts are spaced, doubling from `WIRING_RETRY_INTERVAL_SECONDS` to
the retry loop's cap. After `NATS_MAX_DELIVER` (5) consecutive failures the settle of a
reservation that is not frozen is parked: no automatic attempt, one ERROR line with the
fixed action `wiring_settle_parked`, logged by the settle step itself, and the retry
channels resume. Without parking, a reconcile that fails on one switch's inventory read
would drive another switch's failing rows again on every attempt, with no attempts cap.
A frozen reservation's settle is never parked: a teardown drives only `ACTIVE` rows,
and each failure turns its row `FAILED` for the capped retry channel, so repeating it
cannot loop on a row.

### D9. The terminal teardown

When the terminal handler takes the lease it runs the ledger teardown and then the
dynamic-instance teardown, as on main.

When it meets the lease it defers both, and the settle pass on a frozen reservation
runs both, in main's order: the fold, `_teardown_from_ledgers`, then, when a terminal
event left its `user_id` on the mark, `_execute_dynamic_teardown` with it. A terminal
pass that is interrupted after it took the lease leaves the `user_id` the same way,
since its dynamic teardown has not run either. Revision 2 deferred only the
ledger teardown and let the dynamic teardown run at once, which reversed the order.
Whether a materialized dynamic device can be a switch with ledger rows was not settled
by reading (nothing found forbids it); if it can, a ledger teardown that runs after the
device is deleted reads a 404 and pins its rows unresolvable. Keeping the order makes
the question moot.

The deferral is short. The holder's next renewal reports the freeze, and a pass that is
not a teardown ends there (D5). So the ledger teardown starts within one bounded step
of the holder, plus one iteration of a consumer loop. Main releases every `ACTIVE` row
at once; here that is delayed by at most one driver attempt, and in exchange the holder
stops building wiring the freeze branch would only park.

Both teardowns end their L2 work with the allocation check of D11.

### D10. The owed and parked states reach the owner

A deferred save that is waiting, and above all a parked one, must not be silent.

- The wiring status payload (`internal_wiring_status`, proxied by reservations) gains a
  `settle` object: `none`, `pending` with the time of the request, or `parked` with the
  failure count.
- The Wiring tab shows one line above the rows: for `pending`, that a saved change is
  waiting to be applied; for `parked`, that a saved change could not be applied, with
  the Retry button enabled even when no row is `FAILED`.
- Retry applies it: a manual retry on a reservation that owes a settle, pending or
  parked, takes the lease and runs the settle pass itself before it reattempts rows. A
  success pays the mark; a failure answers 503, as a manual retry's upstream failure
  does today (WIRE-RETRY-12), and counts toward nothing. A save also applies it, since
  the next full pass pays.
- The settle step logs the parked and overdue lines itself, so a deployment with
  `WIRING_RETRY_ENABLED` false still gets them; the tick line repeats the counts where
  the tick runs.

A manual retry holds the lease inside its HTTP request for its whole drive, and now for
a settle pass too. A client or proxy timeout does not release it: the handler runs on,
as it does on main, and the lease is released by its `finally`, or within one term if
the process dies. This record states that and does not bound it.

### D11. Allocations are checked for the whole reservation

`_apply_l2_memberships` checks only the allocations its own removes touched, at its
end, and never reaches that check when it raises midway (Context, item 8). A pass that
is interrupted or fails after removing a membership leaves an allocation that no later
pass will touch.

So, in addition to the check `_apply_l2_memberships` already makes, every full L2
reconcile and every teardown ends by checking every `ACTIVE` allocation of the
reservation, including the early return `_reconcile_l2_memberships` takes when there is
no membership delta:

- on a reservation that is not frozen, an allocation is released when no `ACTIVE`
  membership references it and the reservation has no `FAILED` intended-`ACTIVE`
  membership at all. The second condition is deliberately coarse: a failed join may
  carry a nil allocation until its retry resolves one (WIRE-RETRY-8), so "nothing of
  this reservation is still trying to join" is the only test that cannot release an
  allocation a retry is about to use;
- on a frozen reservation, when no `ACTIVE` membership references it (WIRE-VLAN-4's own
  condition): nothing will join again.

The release stays the compare-and-swap of WIRE-VLAN-4. This is phase 2: it repairs main
without the lease, and the lease design depends on it, because "a pass is all or
nothing" is true of deferral and false of interruption and failure.

### D12. The row claim stays, for good

Revision 2 proposed retiring `claimed_until` in a last phase. That is withdrawn. PR
#1490 sizes an L3 pin's row claim for its whole drive, so one slow step is absorbed by
the slack of the other actions; a per-step lease has no such slack. With every step
bounded (D2) the lease does not need it, but removing a protection main has, in
exchange for deleting code, is the wrong trade in this area. `WiringRowClaims`,
`claim_write_clauses`, `write_guard`, `holder_or_unclaimed`, and `l3_pin_claim_budget`
are not touched by any phase after phase 1. Under the lease a retry pass claims its
rows as it does today, and the two retry channels are excluded twice.

The reconcile, the teardown, and the settle pass still take no row claim and do not
test one, as on main. A dead retry holder's row claim therefore blocks only retries of
that row, for the row budget, and never the reservation's lease.

### D13. Bounds and alarms

- **A dead holder** frees the reservation within one lease term: 5 minutes at the
  defaults, whatever it was driving.
- **A live holder** holds the lease for its whole pass, which has no constant bound:
  one login, one logout, and one action per row or route for each switch, each action
  at most 90.6 seconds at the defaults. One L3 pin of 10 route operations is at most
  about 18 minutes; with issue #1493's proposed limit of 1,000 routes, about 25 hours.
- **A deferred save** is applied by the first full pass that takes the lease after the
  holder ends: the holder's remaining pass, or one term if it died, plus one iteration
  of some replica's consumer loop. The bound is per reservation, the unit a settle
  pays, and one reservation's long hold does not delay another's settle (D8).
- **A deferred teardown** starts within one bounded step of the holder plus one loop
  iteration (D9).
- **No loop.** A settle is requested only by a deferred or interrupted consumer pass or
  with an uncertain record, never by a settle pass that met a lease. A pass that fails
  is tried again at most five times on a reservation that is not frozen, and without
  limit on a frozen one, where no row is driven twice.
- **Alarms.** The settle step logs `wiring_settle_parked`, and one WARNING
  `wiring_settle_overdue` for a mark owed for more than ten minutes while its
  reservation's lease is free. Age alone is not the test: a long L3 drive legitimately
  holds a lease for longer. A lease renewed by one token for more than thirty minutes
  logs `wiring_lease_long_held` once, at INFO. An uncertain record logs one WARNING
  when written.

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
| L2 | success | X `ACTIVE` (`record_l2_membership_active`) | In `remove_keys`: `remove_from_vlan`, `release_l2_membership`; then the allocation check of D11 |
| L2 | failure, or P dies | X `FAILED` intended `ACTIVE` | Stale: park and `remove_from_vlan`, or the direct release when the allocation is nil or unknown |
| L3 | success | X `ACTIVE` with the routes P drove | In `remove_switches`: `remove_route` for the stored routes, `release_route_membership` |
| L3 | partial failure | X `FAILED` intended `ACTIVE`; routes are P's set plus residue it could not remove | Stale: `park_stale_route_build`, then `remove_route` for every stored route |
| L3, still adjacent, routing intent changed | success | X `ACTIVE` with P's older set | In `stay_switches` with a differing pin: the delta reconcile, `record_route_reconciled` |

### A holder is paused past the term, and its write is refused

Holder H made a driver call on key K. A second pass B took the lease while H was paused,
planned from the ledger as H had last committed it, and drove. H resumes; its fenced
write is refused; it writes an uncertain record for K and requests a settle. The settle
pass folds K and plans.

| Layer | What may be on the device | After the fold | What the pass then does |
|---|---|---|---|
| L1, K intended | connected by H, by B, or both | K's row `FAILED` intended `ACTIVE` | Build side: `connect_ports` again, `record_l1_connect`, `ACTIVE`. Device and ledger agree |
| L1, K not intended | connected or not, depending on whose call landed last | K's row `FAILED` intended `ACTIVE` | Stale build: park, `disconnect_ports`, `RELEASED`. The disconnect is driven whatever B recorded, so a connect of H's that landed after B's disconnect is removed |
| L2 | as L1, for a membership | As L1; an inserted row takes the recorded allocation | `add_to_vlan` again, or park and `remove_from_vlan`; then D11 |
| L3, switch adjacent | H's routes r_A, B's routes r_B, in any mixture | The pin stores r_A union r_B (union whatever it stored), `FAILED` intended `ACTIVE` | In `add_switches`: a provision of the intended set I removes the stored routes I does not name, then configures I. Nothing of r_A outlives it |
| L3, switch not adjacent | as above | as above | Stale build: park, then `remove_route` for every stored route |
| Any, frozen | as above | `FAILED` intended `RELEASED` | The release-direction retry removes it while frozen (WIRE-TEARDOWN-4) |

This is the lost-lease L3 case the second review traced: with revision 2, r_A minus r_B
stayed configured with no row naming it, and the teardown, which removes only pinned
routes, left it past the reservation's end.

If H dies after the driver call and before it writes the uncertain record, the change
is unrecorded, as a crash between a driver call and its record is today.

### The reviews' traces against this design

- **Two settle passes, two replicas, an `ACTIVE` row for a removed wire.** One takes
  the lease; the other's select leaves the reservation out, or its take writes no row.
  One driver call.
- **The endless settle loop.** A pass that meets a lease requests nothing.
- **The settle select behind one long-held lease.** Reservation Q's lease is held by a
  retry pass for 18 minutes; Q is the least recently attempted. The select leaves Q
  out, so every replica's step goes to the next owed reservation.
- **A settle pass against a terminal teardown.** One holds the lease; the other defers
  or passes over. One ledger teardown runs.
- **A write after a renewal, after the lease was retaken.** H renews, then is paused
  for longer than the term (the reviews named a synchronous subprocess on the event
  loop and a row-lock wait; D2 bounds both, so what is left is a paused process). B
  takes the lease and plans a park and release of X. H resumes and commits a write for
  X: its fence writes no row and the transaction rolls back. X is as B read it. H's
  driver call becomes an uncertain record, traced above.
- **The allocation leak.** `_apply_l2_memberships` removes M1 on switch S1; the read for
  S2 raises. The redelivery, or the settle pass, runs the full L2 reconcile and ends
  with D11's check over every `ACTIVE` allocation of the reservation: A has no `ACTIVE`
  member and nothing is still trying to join, so A is released. If the reservation ends
  first, the teardown's check releases it.
- **R1 and R3.** The pass re-reads its rows and the fork under the lease.
- **R2.** The carried-delta apply meets P's lease and is deferred; the settle pass
  releases the row P left `ACTIVE`.
- **The nacked version.** Last applied is 3; v4 is nacked; v5 is deferred. The settle
  pass applies the fork at v5 and stamps 5; v4 arrives stale. If instead v4 was
  deferred and v5 nacked, the stamp is 4 and v5 arrives contiguous, its delta a set of
  no-ops against state already at v5.
- **A reservation ends while a retry pass is building.** The handler freezes, requests
  a settle with the `user_id`, and is acked. The retry pass's next renewal reports the
  freeze; it stops, closes its run row, logs out. A success it had already made is
  parked by the record-time freeze branch (WIRE-LEDGER-4). The settle pass runs the
  ledger teardown, then the dynamic teardown.
- **A consumer dies holding the lease.** The message is redelivered after `ack_wait`;
  the redelivery meets the dead holder's lease, requests a settle, and is acked; the
  settle step takes the lease once the term has passed.

### Where it does not converge, or converges only with a condition

- **A process paused for longer than the lease term** lets a second pass drive the same
  reservation for the length of one driver attempt. The ledger stays truthful (D3) and
  the device is re-driven to intent (D6), provided the paused holder lives to write its
  uncertain record.
- **A parked settle.** The deferred save is not applied until the owner presses Retry
  or saves again. It is shown (D10).
- **The stamp below the fork's version** (D8): a wire can be rebuilt until a later
  event lands. As on main.
- **A failed L1 or L2 build on a frozen reservation** stays `FAILED` intended `ACTIVE`;
  if the failed call did land on the device, nothing removes it (WIRE-TEARDOWN-6, last
  sentence). Existing decision.
- **A frozen park keeps the attempt count** (WIRE-LEDGER-4): a row at the cap waits for
  a manual retry. Existing behaviour.
- **The cross-reservation writers** (WIRE-LEDGER-11, WIRE-LEDGER-12) act on another
  reservation's `ACTIVE` row without its lease, and two reservations' passes can drive
  one switch at once. As today.
- **An allocation of a live reservation with a failed join at the attempts cap** is not
  released by D11's coarse test while that row exists. It is released when the row is
  built, parked, or the reservation ends.

## Why this does not repeat the failures

| Failure | Invariant it broke | Its precondition | Why it cannot recur here |
|---|---|---|---|
| Round 1: a guarantee narrower than stated | E2: writers outside the guarded set wrote | Writers were guarded one by one, and some were missed | The fence is in the session factory a pass gets with its lease, so it covers every commit, and every wiring driver call passes one function that refuses to run without the lease |
| Round 2: stamp guard on the holder's success flips | C2: routes configured, record refused, nothing owed | A writer that never claims cleared the holder's stamp, and a refused write had nowhere to go | No pass writes a reservation's rows without its lease, so nothing clears it; and a refused write that recorded a driver call becomes an uncertain record that the next full pass must fold before it plans |
| Round 3: `holder_record` PARK and REOPEN | E2: PARK wrote, by key, a row the holder never held, and released a wire the fork intends | A superseded holder decided from the row as it found it | A superseded holder writes no ledger row at all. The fold is made by the lease holder, and on a live reservation it never points a row toward release: the fork, read under the lease, decides |
| Revision 1: row claims plus a settle mark | E1 across replicas; C1 for a request made too late | A pass skipped some rows and went on with others | A pass holds the lease or defers before doing anything |
| Revision 2: a lease checked before the write | E2: a stalled holder wrote under the next lease; C2 on L3 | The check and the write were two statements; success flips were not checked at all | The renewal is the last statement of the write's own transaction (D3); every refused record of a driver call is kept (D6) |
| Main today (#1481, R2, R3) | E1, E2, and a plan made before the claim | The reconcile and teardown never claim; retry plans predate the claim | D1 and D4 |

## Invariants and what will pin them

Test names are proposals for the implementing pull requests; none exists yet.

| Invariant | To be pinned by |
|---|---|
| Two takes of one lease: exactly one writes a row; an expired lease is retaken; a missing state row is inserted by the take, in one statement | unit; Postgres-live with two connections |
| A ledger write whose pass lost the lease after its last renewal is rolled back, for a failure write, a park, and a success flip, per layer | Postgres-live: the holder is held at its commit by an event, the lease is retaken, the holder is released |
| A refused write that recorded a driver call leaves one uncertain record and one settle request; a refused park leaves neither | unit |
| The fold, per layer and per freeze state, and the pass after it | unit; Postgres-live for the L3 route union |
| The expiry is written and compared with the database clock | Postgres-live only: `clock_timestamp()` does not exist on SQLite, where the unit tests inject the clock |
| A consumer pass that meets a lease makes no wiring write but the request, and returns | unit on `handle_wiring_changed` and the terminal handler, asserting the statements made |
| A settle select leaves out a reservation whose lease is live; a lost take stamps the attempt time | unit; Postgres-live with one reservation held and two owed |
| Paying is an update that only raises, to the s read under the lease | unit; Postgres-live with a request staged during the pass |
| A full pass stamps the deferred version; R4 | unit |
| A retry pass plans from rows re-read under the lease | unit: R1 and R3 as written by the reviews, failing on main and passing here |
| A renewal that reports a freeze ends a pass that is not a teardown, with its run row closed and its session logged out | unit |
| A deferred terminal event runs the ledger teardown and then the dynamic teardown, with the event's `user_id` | unit |
| The allocation check releases an allocation orphaned by a raise mid-apply, and keeps one a failed join still needs | unit, `test_nats_consumer_vlan_definitions.py` pattern |
| Every await between two renewals has a bound below the term, at the smallest and largest `EXECUTION_TIMEOUT_SECONDS` | unit, one test per await and one on the term's floor |

## Options rejected

| Option | Complexity | Why not, with the trace |
|---|---|---|
| Revision 1: the reconcile takes the row claim and skips held rows inside its pass | High | The first review's eleven defects. It needs a reservation lease anyway, for `ACTIVE` rows and keys with no row, and then seven further mechanisms |
| The holder re-checks intent after its drive, with no settle pass | Medium | C1 stays open in two traces. The holder's fork read fails after its success flip (cabling answers 5xx): it cannot judge, the row is `ACTIVE`, nothing is owed. Or the holder dies between the flip and the re-check |
| Issue option 1 (parks keep the stamp) plus a guard on the key-based failure write | Low | E1 stays open on an unrelated save: the build diff is desired minus `ACTIVE`, so a save that touches nothing near row X still puts X's still-desired key on the build side and the reconcile drives it beside the holder (effect 3). The owner rejected option 1 alone |
| Issue option 3: the reconcile waits for the holder | Low | The consumer handles one message at a time, so one held reservation stalls every reservation's events on that replica. The owner rejected it |
| Settle by delayed redelivery of the same event | Medium | Four redeliveries over 81 seconds, shared with real upstream failures; the fifth failure dead-letters a healthy event that is not staged again |
| Settle by a follow-up event through execution's outbox | Medium | The outbox has no delay; execution would publish on a subject reservations owns (RES-EVENT-2); a follow-up at a stamped version is a no-op |
| Ask reservations to stage a heal | Medium | A cross-service round trip for state execution holds itself, and it cannot settle a frozen reservation |
| The settle step in the retry tick | Low | It needs an in-process guard against the consumer (a stall, and a lock that is not reentrant), and it runs only where `WIRING_RETRY_ENABLED` is true |
| One lease stamp sized for the whole pass | Low | A pass has no bound, so neither does the stamp: a holder that dies blocks its reservation for hours |
| A background task that renews the lease while a step runs | Medium | It keeps a stuck pass's lease alive, so it needs a watchdog on top; bounding each await does the job with no second task |
| Renew, then write (revision 2) | Low | Check-then-act; the second review's trace under "A write after a renewal" |
| Success flips exempt from the fence (revision 2) | Low | A superseded holder's flip interleaves with the new holder's park, which is a select and an update, and the row can end `ACTIVE` with intended `RELEASED` |
| Refuse a superseded holder's write and stop | Low | Round 2: the device change is recorded nowhere |
| Retire the row claim (revision 2, phase 4) | Low | It removes the whole-drive slack PR #1490 gives an L3 retry (D12) |
| `holder_record` again | High | Round 3 |

The trade the lease makes against a row-granular claim: a save that lands while any
pass is driving its reservation is applied whole by the settle step, seconds after that
pass ends, instead of at once except for the held rows.

## Spec rule edits for the implementing pull requests

`docs/specs/provisioning-and-wiring.md`; not edited by this record. Each phase changes
the rules it makes untrue in its own pull request; this is the union.

- **Header.** Add ADR 0018 to the Design records row.
- **Section 4.** The wiring state table gains the lease (take, renew, release) and the
  settle counters (request, pay). A new table for the uncertain record (written,
  folded). Assignment row transitions gain the fold (`ACTIVE` or `FAILED` to `FAILED`).
  Concurrency: every pass excludes every other pass of its reservation with the drive
  lease, and every ledger write is fenced by it.
- **WIRE-CLAIM-1** (phase 1). Add: the update also requires the row's `intended` to
  equal the direction the call site drives; a claim lost for that reason does not join
  `lost`, and the row reads back by its status (`still_failed`), not `in_progress`.
- **WIRE-CLAIM-3**, as rewritten by PR #1490: unchanged; add that the row budget
  excludes two retry passes on one row and is not what excludes a reconcile.
- **WIRE-CLAIM-4.** Proposed wording: "A retried row is claimed immediately before its
  own driver call (an L3 pin once, before its switch's login, with the budget of
  WIRE-CLAIM-3), never at selection. The reconcile, the terminal teardown, and the
  settle pass take no row claim: each holds its reservation's drive lease for the whole
  pass (WIRE-LEASE-1), and so does a retry pass, which claims its rows under it."
- **WIRE-CLAIM-8, WIRE-CLAIM-9.** The limit about the reconcile becomes a reference to
  the lease; the holder's flips are fenced by the lease (WIRE-LEASE-4) and still do not
  consult the row stamp.
- **WIRE-VLAN-4** (phase 2). The reservation-wide check and its two conditions.
- **New section 8.16, WIRE-LEASE-1 to WIRE-LEASE-7.** Who takes the lease; take, renew,
  release; the term and the bounded steps; the fenced commit; deferred, interrupted,
  failed; the freeze ending a pass; the database clock.
- **New section 8.17, WIRE-SETTLE-1 to WIRE-SETTLE-8.** The request; the uncertain
  record and the fold; where the settle step runs; selection; what it runs by
  reservation state; paying and the stamp; spacing and parking; the alarms.
- **WIRE-CONSUME-2.** One settle pass at most between fetches.
- **WIRE-ORDER-3, WIRE-ORDER-4, WIRE-ORDER-10.** A `wiring_changed` that meets the
  lease requests a settle and is not stamped; a full pass stamps the deferred version;
  an uncertain record forces the full path.
- **WIRE-DISPATCH-3, WIRE-TEARDOWN-1 to WIRE-TEARDOWN-3.** A terminal event that meets
  the lease freezes, requests a settle, and leaves both teardowns, in order, to the
  settle pass.
- **WIRE-RETRY-5, WIRE-RETRY-6, WIRE-RETRY-11, WIRE-RETRY-12, WIRE-RETRY-13,
  WIRE-RETRY-14, WIRE-RETRY-16, WIRE-RETRY-19.** A retry pass takes the lease per
  reservation and re-reads its rows under it; `in_progress` also means the reservation
  is leased or owes a settle; the manual retry applies an owed settle; the selects'
  third exclusion; the tick line's counters.
- **Section 8.15, wiring status.** The `settle` object and the Wiring tab's line.
- **Section 13.** Remove the two #1481 limits; add the limits under "Where it does not
  converge".

Also `CHANGELOG.md`, `docs/specs/CONVENTIONS.md` (the lease and the leased session
factory as the one helpers), the driver-load and sandbox rules where phase 2 bounds
them, `docs/USER_GUIDE.md` for the Wiring tab, the module docstring of
`wiring_claim.py`, the docstrings of the three applies, and the docstring of
`record_route_reconciled`.

## Test plan

At HERD's five levels.

1. **Unit** (SQLite, `services/execution/tests/`). The invariants table. Phase 1: the
   claim per `intended` state at each of its three call sites, and the read-back. R1 to
   R4 enter the suite as the reviews wrote them: R1 passes at phase 1, R2 and R3 at
   phase 6, R4 already passes and pins what D8 relies on. Phase 2: the allocation leak
   on a raise mid L2 apply, failing on main; each bounded await; the term at
   `EXECUTION_TIMEOUT_SECONDS` 1 and 3600, with and without the install allowance. The
   upsert on a missing state row, for the take and for the request. Tests that pin main
   and change on purpose at phase 6: the three
   `test_after_the_reconcile_clears_the_claim_*` tests (a reconcile can no longer run
   beside the holder).
2. **Functional** (the service's real surface, in process). The internal retry route:
   `in_progress` for a leased reservation; an owed settle applied by Retry; 503 when
   that settle fails. The wiring status payload's `settle` object in each state.
   `handle_reservation_event` with a real payload for a deferred save and a deferred
   terminal event, asserting the dynamic teardown has not run. The consumer stall: with
   a lease held by another pass the handler returns after one statement, and a second
   reservation's event is handled without delay.
3. **Postgres-live** (the `_gated_driver_stub` and `_loser_inside_holders_drive`
   pattern of `test_wiring_retry_claim_race_live_pg.py`; every overlap staged with
   events, every wait bounded, no sleep):
   - a save lands while a retry pass is inside its drive, once each for an L1 connect,
     an L2 join, and an L3 provision: one driver call for the row, a request recorded,
     the version not stamped; then the holder succeeds, and separately fails, and the
     settle pass releases the row and stamps;
   - a write lands after a successful renewal and after the lease was retaken, for a
     failure write, a park, and a success flip: nothing of it is visible, and the
     success leaves an uncertain record that the next pass folds;
   - the L3 case of that: the first holder's routes are removed by the provision after
     the fold;
   - the settle select with one reservation's lease held and two others owed: both are
     settled while the first is still held, and a lost take stamps its attempt time;
   - two settle steps on two connections for one reservation: one runs;
   - a settle step against a terminal teardown, both orders: one ledger teardown runs;
   - a terminal event lands while a retry pass is inside an L3 provision: the pass
     stops at its next renewal, and the test measures how many driver calls the holder
     made after the freeze (at most the one in flight) and that the ledger teardown ran
     before the dynamic teardown;
   - a consumer that dies holding the lease: the redelivery defers, and the settle step
     runs after the test moves the expiry into the past;
   - a step stretched three ways: by a thread pool with no free worker (the gap check
     inside the worker refuses to start), by a held row lock (`lock_timeout` raises),
     and by a slow driver download (the total deadline raises); in each the lease is
     not lost and no driver call overlaps;
   - the database clock: a process whose own clock is an hour off still excludes and is
     excluded. Postgres-live is the only level that can pin this.
   The two live tests that pin main,
   `test_teardown_during_a_held_l3_provision_leaves_its_routes_recorded_for_removal` and
   `test_a_reconcile_failure_write_during_a_held_l1_connect_still_records_it`, are
   rewritten at phase 6. Each fixed test carries its mutation proof: the take made
   unconditional, the fence removed from the commit, the uncertain record not written,
   the fold skipped, the live-lease exclusion removed from the select, the pay made
   unconditional, the re-read under the lease removed.
4. **Integration** (running stack, mock drivers). The L1, L2, L3, and ledger teardown
   suites stay green. One convergence test with the mock drivers' `HERD_mock_sleep_ms`
   knob holding a retry open while a save lands, asserting the end state by polling the
   wiring status, never by timing. It is the only level that proves the settle step
   runs in a real consumer loop.
5. **Browser end to end** (phase 5). The Wiring tab's line for an owed and for a parked
   settle, and Retry applying a parked one. Frontend lanes cannot run e2e on the
   development host, so the nightly is the proof.
   **Load.** No new suite: no new route. `make test-load` must stay green; its
   concurrent saves exercise deferral from phase 6, and the test asserts the deferral
   count is reported, not that it is zero. The settle step's cost when nothing is owed
   is one indexed select per fetch, pinned by a unit test that counts statements.

Virtual-clock tests cannot settle the lease term: they do not model queueing or a
blocked event loop. The term follows from the bounds of D2, and each bound has its own
test above.

## Delivery phases

Each pull request is mergeable alone and leaves main correct.

| Phase | Pull requests | Scope | Contents |
|---|---|---|---|
| 1 | 1 | small | `WiringRowClaims.claim` compares `intended` with the direction its call site drives (connect, join, and provision expect `ACTIVE`; their removals expect `RELEASED`), so there is no new plumbing. A claim lost for that reason goes to its own set, not `lost`, and reads back `still_failed` |
| 2 | 2 | small, and small to medium | (a) The reservation-wide allocation check (D11). (b) Bounded steps: total deadlines on the driver download and the internal reads, the schema extraction off the event loop, the process-group kill, a `finally` around each switch for the run row and the logout |
| 3 | 2 | medium | The machinery, inert. (a) The migration (lease, mark, uncertain table); take, renew, release; the leased session factory with the fenced commit and `lock_timeout`; the context variable, the renewal points, the gap check inside the worker. (b) The reconcile body factored into one function, with the fold. Nothing takes a lease, so nothing changes |
| 4 | 1 | large | The consumer's passes take the lease: deferred, interrupted, failed; the settle step; the stamp; spacing and parking; the deferred teardown in main's order; the freeze ending a pass; the `settle` object in the wiring status payload |
| 5 | 1 | medium, backend and frontend | The Wiring tab's line; Retry applying an owed settle. Ships in the same release as phase 4, so a parked settle is never invisible in a release |
| 6 | 3 | medium each | Retry passes take the lease and plan under it, one layer per pull request; the selects' new exclusion; the manual retry's `in_progress`. The refusal of a wiring driver call with no lease in context is switched on with the last layer |

Ten pull requests. In the owner's own cost table a medium lane with an adversarial
review and a fix round is 20 to 25 session points, so phases 3 to 6 are an arc of the
size of ADR 0009's, not a week's work.

Phase 3 must precede phase 4, and phase 4 phase 6. Phases 1 and 2 are independent of
the rest and of each other. In a deployment with one consumer replica, phases 4 and 5
defer nothing in normal running: the consumer is serial and retry passes do not hold
the lease yet; the one case is a redelivery that meets the lease of a pass that died.

## Consequences

- A save, or a reservation's end, that lands while any pass is driving that reservation
  is applied by the settle step after that pass ends, not at once. For a reservation's
  end that is at most one driver attempt later.
- A manual retry answers `in_progress` for a whole reservation that is leased, and
  applies an owed settle before it retries rows.
- One migration: columns on `reservation_wiring_state` and one small table. No new
  setting, route, event, or subject. The wiring status payload gains one object, and
  the Wiring tab one line.
- Every commit a pass makes carries one more update, and every driver attempt and
  upstream read one short update on the lease.
- A row can move from `ACTIVE` to `FAILED` by the fold. Nothing did that before.
- A reservation whose settle keeps failing is parked after five attempts and shown to
  its owner.
- Tests that pin main's behaviour change deliberately; the test plan lists them.

## Open questions for the owner

Each has this record's recommendation.

1. **Whether to build phases 3 to 6 at all, and at reservation granularity.** The first
   section states the case both ways. Recommendation: ship phases 1 and 2 now; decide
   3 to 6 as one arc. If the answer is no, record #1481 as a limit with R2 and R3 named.
   If yes, accept the lease: the row-granular form needs a reservation lease anyway.
2. **Parking.** Five failed attempts over about fifteen minutes parks an owed settle, so
   an inventory or cabling outage of that length parks every one that is owed.
   Recommendation: keep five, mirror `max_deliver`, and rely on D10 to make it visible
   and one press to apply. The alternative is to keep trying at the capped spacing,
   which re-drives failing rows with no attempts cap for as long as the outage lasts.
3. **Should a refused failure write leave an uncertain record?** D6 says yes, because a
   call that timed out may have landed. The cost is an extra driver call on a key that
   usually did not change. Recommendation: yes.
4. **The stamp.** Cabling's internal fork answer carries the fork's versions
   (`ForkDetailResponse.versions`). If a save commits its version row and its
   connections together (`fork_save_service.py`; not verified here), every full
   reconcile could stamp the fork's own version and close the rebuilt-wire gap under
   D8, which main has today. Recommendation: verify it; if it holds, do it in phase 4.
5. **Dynamic devices as switches.** Nothing read forbids a dynamic template whose
   device is a switch with ledger rows. D9 keeps main's order so it does not matter
   here. Recommendation: decide separately whether inventory should refuse such a
   template, since main's own teardown order is the only thing protecting it.
6. **The tick's lease scope.** Per reservation (recommended), or once for the tick's
   whole batch, which is less code and defers saves for every reservation in the batch.
7. **Deferral latency.** A save is delayed whenever anything is driving its
   reservation. Recommendation: accept, and watch the deferral count; if it matters,
   the holder can run the settle pass itself before it releases, as a later change.
8. **Issue #1413.** Its interleaving needs two passes of one reservation at once, which
   the lease excludes after phase 6. Recommendation: have that issue's owner confirm it
   against this record before closing it.

## Revisit when

- a step that a pass makes cannot be bounded below the lease term (a driver action with
  no timeout);
- the consumer handles more than one message at a time (the settle step then needs its
  own place);
- wiring gains a writer that is not one of the four entry points;
- deferrals become common enough to notice (open question 7);
- two reservations driving one switch at once becomes a problem: the lease is per
  reservation and says nothing about it.

## Revision history

- Revision 1, commit `6a6b912e`: row-granular claim for the reconcile, a settle mark,
  and a settle step in the retry tick.
- Revision 2, commit `9f443621`: a reservation drive lease for every pass, the settle
  step in the consumer loop, the plan made under the lease.
- Revision 3: ledger writes fenced in their own transaction; the uncertain record and
  the fold for a refused write; deferred, interrupted, and failed defined; the settle
  select excludes live leases; the terminal teardown deferred whole and in main's
  order, and a freeze ends a pass; the owed and parked states shown, and Retry applies
  them; the reservation-wide allocation check; every step bounded and the term set to
  the one-row budget; the row claim kept for good; sizes per phase.
- Revision 4: merged with main; added the list of what has shipped since the record was
  written; no decision changed.
