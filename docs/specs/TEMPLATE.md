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
visibility, as a numbered rule in section 5.

## 3. Concepts and data

The nouns of this area. One row per concept: what it is, which service owns it, and
where it is stored. Note every reference to another service's data, because those
are bare identifiers with no foreign key.

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|

## 4. State model

Only for areas with a lifecycle. Otherwise write "None."

List the states, then every transition. A transition that is not in the table does
not exist.

| From | To | Trigger | Guard | Side effects |
|---|---|---|---|---|

State how a transition is made safe against a concurrent writer.

## 5. Features

One subsection per feature. Use the feature names from `FEATURES.md` where they
exist, so a reader can move between the two documents.

### 5.N Feature name

**What it does.** One or two plain sentences: what a user can do and what they see.

**Surfaces.**

| Surface | Where |
|---|---|
| User interface | page or component, as a repository path |
| API | method and path, with the owning service |
| Events | subjects published or consumed |
| Background work | the task or sweep, and its interval setting |

**Rules.**

Each rule is one checkable statement with its enforcement and its test.

- **XXX-TOPIC-1.** The statement.
  - Enforced in: `path/to/file.py` (`symbol_name`)
  - Pinned by: `path/to/test_file.py` (`test_name`)
- **XXX-TOPIC-2.** The statement. By decision; see ADR NNNN or issue #NNN.
  - Enforced in: `path/to/file.py` (`symbol_name`)
  - Pinned by: none (listed in section 9)

**Errors.**

What a caller receives when a rule refuses a request. Give the exact status and the
shape of the body, because clients depend on both.

| Condition | Status | Body | Rule |
|---|---|---|---|

**Out of scope.** What this feature deliberately does not do, so a reader does not go
looking for it.

## 6. Interactions with other services

Every call this area makes to another service or driver, and every event it
publishes or consumes. The failure column is required.

| Direction | Peer | Call or event | Purpose | On failure |
|---|---|---|---|---|

"On failure" says fail open or fail closed, and what the original caller sees.

## 7. Configuration

Settings that change this area's behavior. Give the name, the default, and the
effect; link `docs/ENV_VARS.md` for the rest.

| Setting | Default | Effect |
|---|---|---|

## 8. Test coverage map

Where this area is tested at each of the five levels. Write "Does not apply" with a
reason where a level has nothing to test, and "None" where it should have a test and
does not.

| Level | Where | Notes |
|---|---|---|
| Unit | | |
| Functional (through the service API) | | |
| Integration (running stack) | | |
| Stress and load | | |
| Browser end-to-end | | |

## 9. Known limits and gaps

Everything a reader should not assume. Each entry links an issue where one exists.

- **Limits by decision:** behavior that is deliberate and will stay.
- **Open defects:** behavior that is wrong today.
- **Unpinned rules:** every rule in section 5 marked `Pinned by: none`.
- **Not verified:** anything in this document that could not be checked against a
  running system or a test, and why.
