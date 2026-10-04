# HERD specifications

One specification per feature area. Each one answers two questions for a reader who
is looking at the code and wants to know what it is supposed to do:

- **Feature specification:** what the feature is for, who uses it, and what they can
  do with it.
- **Functional specification:** the exact rules the system follows, and where each
  rule is enforced and tested.

Both live in the same document so they cannot drift apart.

## What a specification is, and is not

A specification states what HERD does **today**, verified against the code and the
tests at the commit named in its header. It is descriptive, not aspirational.

| Question | Read |
|---|---|
| What does this feature do, exactly? | The specification in this directory |
| Why was it designed this way? | The ADRs in [`../design/`](../design/) |
| How do I use it? | [`../USER_GUIDE.md`](../USER_GUIDE.md), [`../ADMIN_HANDBOOK.md`](../ADMIN_HANDBOOK.md), the published manual |
| How do the services fit together? | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) |
| Which role may call which endpoint? | [`../ROLES.md`](../ROLES.md) |
| What shipped, and when? | [`../../FEATURES.md`](../../FEATURES.md), [`../../CHANGELOG.md`](../../CHANGELOG.md) |
| What is planned? | [`../../PLANNED_FEATURES.md`](../../PLANNED_FEATURES.md) |

A specification links to those documents instead of repeating them. It never
restates the full permission matrix, an ADR's argument, or a how-to.

## Index

| Area | Specification | Status |
|---|---|---|
| Identity and access | `identity-and-access.md` | Not written |
| Inventory | `inventory.md` | Not written |
| Topology | `topology.md` | Not written |
| Reservations | [`reservations.md`](reservations.md) | Pilot |
| Provisioning and wiring | `provisioning-and-wiring.md` | Not written |
| Dynamic resources | `dynamic-resources.md` | Not written |
| AI features | `ai-features.md` | Not written |
| Device configuration | `device-configuration.md` | Not written |
| Operations and observability | `operations-and-observability.md` | Not written |
| Integration | `integration.md` | Not written |

The areas follow the headings of `FEATURES.md`. Provisioning and wiring is split out
of Reservations because it is large enough to need its own document.

## Conventions

These rules are what make a specification trustworthy. A document that breaks them is
not finished.

1. **As-built.** Every statement describes behavior that exists at the commit in the
   header. Read the code and the tests; do not write from memory, from an ADR, or
   from another document. An ADR records a decision at a point in time and the code
   may have moved since.
2. **Every rule is numbered and has a stable identifier.** The form is
   `<AREA>-<TOPIC>-<N>`, for example `RES-STATUS-3`. Identifiers are never reused or
   renumbered; a removed rule keeps its number and is marked withdrawn. Code
   comments, tests, issues, and reviews can then cite a rule by name.
3. **Every rule names where it is enforced and what pins it.** "Enforced in" gives a
   repository path and a symbol (a function, class, or constant). "Pinned by" gives a
   test file and, where one test carries the rule, the test name. Do not use line
   numbers: they go stale on the next edit.
4. **A rule with no test says so.** Write `Pinned by: none` and list the rule under
   Known limits and gaps. An unpinned rule is a finding, not something to hide.
5. **A rule states one checkable fact.** "Cancel is refused on a finished
   reservation" is a rule. "Cancellation is handled robustly" is not.
6. **Say what happens on failure.** For every call to another service or to a driver,
   state whether the feature fails open or fails closed, and what the caller sees.
7. **Record decisions as decisions.** Where the behavior is a deliberate choice that
   a reader might mistake for a bug, say "by decision" and link the ADR or issue.
8. **Defects are not smoothed over.** If the code does something that looks wrong,
   the specification states what it does, flags it under Known limits and gaps, and
   links an issue. It does not describe the behavior you wish it had.
9. **Plain words first.** Each feature opens with what a user can do, in a sentence a
   non-engineer can follow. Identifiers and code names come after.
10. **Repository style applies.** No em-dashes, no arrows (write "to"), no
    box-drawing characters, no emojis. Tables use markdown table syntax.

## Keeping a specification current

- A change that alters behavior a specification describes updates that specification
  in the same pull request, the same way it updates `CHANGELOG.md`.
- `tests/unit/test_spec_references.py` fails when a specification names a repository
  path that does not exist, so a rename or a deleted test cannot leave a dangling
  reference unnoticed. It also fails when an "Enforced in" or "Pinned by" symbol no
  longer appears in the file it names, when a rule identifier repeats or a rule lacks
  either line, when a relative link does not resolve, or when a template section is
  missing. Its module docstring defines exactly what counts as a path, a reference
  line, and a rule. Write references as `` `path` (`symbol`, `symbol`) `` groups
  separated by `;`, and `Pinned by: none` for an unpinned rule. A withdrawn rule says
  "withdrawn" in its text and needs no references.
- The header's "Verified at" line names the commit the document was last checked
  against in full. A partial update leaves that line alone.

## Writing a new specification

Copy [`TEMPLATE.md`](TEMPLATE.md), keep every section heading, and write
"None." under a section that does not apply instead of deleting it. A missing section
reads as an oversight; an explicit "None." reads as an answer.
