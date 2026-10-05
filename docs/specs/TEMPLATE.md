# AREA NAME specification

| | |
|---|---|
| Area prefix | `XXX` (used in rule identifiers, for example `XXX-TOPIC-1`) |
| Verified at | commit `0000000` (`vX.Y.Z-N-g0000000`), YYYY-MM-DD |
| Owning services | the services that hold this area's data and rules |
| Other services involved | the services this area calls or is called by |
| Design records | the ADRs under `docs/design/` that explain why |
| Related guides | user, admin, and operator documents for this area |

## 1. Purpose

Two to four sentences. What problem this area solves for a person running or using a
lab, and what it does not try to do.

## 2. Actors and permissions

Who takes part, and what each may do, at the level of intent. Link `docs/ROLES.md`
for the endpoint matrix instead of repeating it.

| Actor | May | May not |
|---|---|---|
| User | | |
| Admin | | |
| Superadmin | | |
| Another service (internal token) | | |

State any rule that goes beyond role, for example ownership or device-group
visibility, as a numbered rule in section 8.

## 3. Concepts and data

The nouns of this area. One row per concept: what it is, which service owns it, and
where it is stored. Note every reference to another service's data, because those
are bare identifiers with no foreign key.

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|

## 4. State model

Only for areas with a lifecycle. Otherwise write "None."

This is the ONLY section that states a status transition. A feature section that
causes a transition cites the rule id here; it never restates the transition.

**Statuses.** List each status and what it means, one line each.

**Transitions.** One row per allowed transition. A transition that is not in the
table does not exist.

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|

"Performed by" names the route, task, or callback that makes the write. "Stages"
names the event written in the same transaction, or "nothing".

**Concurrency.** How a transition is made safe against a concurrent writer.

**Rules.** The numbered state rules the table cites, in the rule format of section 8.

## 5. API surface

Every user-facing route this area serves. Feature sections refer to a row by method
and path; they do not repeat it.

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|

"Who may call" is the role or ownership condition in plain words. "Success" is the
status code. "Rules" lists the rule ids that govern the route; every id must exist in
this document.

## 6. Events

Every event this area publishes. Write "None." when it publishes nothing.

| Subject | Producer | Staged when | Consumers | Payload keys | Rules |
|---|---|---|---|---|---|

## 7. Internal API

Routes this area serves to other services, not to users. Write "None." when there
are none.

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|

## 8. Features

One subsection per feature. Use the feature names from `FEATURES.md` where they
exist, so a reader can move between the two documents.

### 8.N Feature name

**What it does.** One or two plain sentences: what a user can do and what they see.

**Surfaces.** The user interface (a repository path) and any background work (the
task and its interval setting). Name the routes and events by method and path or by
subject; their details live in sections 5 to 7.

**Rules.**

Each rule is one checkable statement followed by exactly two reference lines. A
reference line is indented two spaces, has no bullet, and fits on one line: a path in
backticks, then its symbols or test names in parentheses, several path groups
separated by `; `.

- **XXX-TOPIC-1.** The statement.
  Enforced in: `path/to/file.py` (`symbol_name`, `other_symbol`); `path/to/other.py` (`symbol`)
  Pinned by: `path/to/test_file.py` (`test_name`)
- **XXX-TOPIC-2.** The statement. By decision; see ADR NNNN or issue #NNN.
  Enforced in: `path/to/file.py` (`symbol_name`)
  Pinned by: none (issue #NNN)

**Out of scope.** What this feature deliberately does not do, so a reader does not go
looking for it.

## 9. Errors

Every error a caller of this area can receive, in one table. Give the exact status
and the shape of the body, because clients depend on both.

| Status | Error key or detail | When | Rule |
|---|---|---|---|

## 10. Interactions with other services

Every call this area makes to another service or driver. The failure column is
required. Events are listed in section 6, not here.

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|

"On failure" says fail open or fail closed, and what the original caller sees.

## 11. Configuration

Settings that change this area's behavior. Give the name, the default, and the
effect; link `docs/ENV_VARS.md` for the rest.

| Setting | Default | Effect |
|---|---|---|

## 12. Test coverage map

Where this area is tested at each of the five levels. Write "Does not apply" with a
reason where a level has nothing to test, and "None" where it should have a test and
does not. End with what was not run while writing this document.

| Level | Where | Notes |
|---|---|---|
| Unit | | |
| Functional (through the service API) | | |
| Integration (running stack) | | |
| Stress and load | | |
| Browser end-to-end | | |

## 13. Known limits and gaps

Everything a reader should not assume, in three separate lists. Write "None." under a
list that is empty.

### Open defects

Behavior that is wrong today. Each entry names the rule it affects and its GitHub
issue (`#NNN`); the issue, not this document, says what the fix should be.

### Limits by decision

Behavior that is deliberate and will stay. Each entry cites the document or issue
that records the decision.

### Rules with no test

Every rule marked `Pinned by: none`, and nothing else.
