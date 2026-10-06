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
| Identity and access | [`identity-and-access.md`](identity-and-access.md) | Written |
| Inventory | [`inventory.md`](inventory.md) | Written |
| Topology | [`topology.md`](topology.md) | Written |
| Reservations | [`reservations.md`](reservations.md) | Pilot; the worked example of the template |
| Provisioning and wiring | [`provisioning-and-wiring.md`](provisioning-and-wiring.md) | Written |
| Dynamic resources | [`dynamic-resources.md`](dynamic-resources.md) | Written |
| AI features | [`ai-features.md`](ai-features.md) | Written |
| Device configuration | `device-configuration.md` | Not written |
| Operations and observability | `operations-and-observability.md` | Not written |
| Integration | `integration.md` | Not written |

The areas follow the headings of `FEATURES.md`. Provisioning and wiring is split out
of Reservations because it is large enough to need its own document.

Every specification has the same thirteen sections, in this order: Purpose, Actors and
permissions, Concepts and data, State model, API surface, Events, Internal API,
Features, Errors, Interactions with other services, Configuration, Test coverage map,
and Known limits and gaps. The cross-cutting tables (sections 4 to 7 and 9) come
before or after the features so each fact has one home; [`TEMPLATE.md`](TEMPLATE.md)
gives the columns of each.

## Conventions

These rules are what make a specification trustworthy. A document that breaks them is
not finished.

1. **As-built.** Every statement describes behavior that exists at the commit in the
   header. Read the code and the tests; do not write from memory, from an ADR, or
   from another document. An ADR records a decision at a point in time and the code
   may have moved since.
2. **Every rule is numbered and has a stable identifier.** The form is
   `<AREA>-<TOPIC>-<N>`, for example `RES-STATUS-3`: the area prefix from the header,
   a topic word, and a number. Identifiers are never reused or renumbered. A rule that
   moves to another section keeps its identifier, so the topic word names where the
   rule started, not where it lives now. A removed rule keeps its number and is marked
   withdrawn. Code comments, tests, issues, and reviews can then cite a rule by name.
3. **Every rule names where it is enforced and what pins it, on one line each.**
   Directly under the rule, indented two spaces and with no bullet, write
   `` Enforced in: `path` (`symbol`, `symbol`); `path` (`symbol`) `` and
   `` Pinned by: `path` (`test_name`) ``. End the last line of the rule's text and the
   "Enforced in" line with a space and a backslash (the markdown hard line break), so
   the rule, its enforcement, and its tests render on three lines. A symbol is a function, class, or constant
   that appears in that file; a test name is the test function, or for a frontend test
   the exact test title. Do not use line numbers anywhere in a specification: they go
   stale on the next edit.
4. **A rule with no test says so.** Write `Pinned by: none`, optionally followed by
   the issue in parentheses, and list the rule under "Rules with no test" in the gaps
   section. An unpinned rule is a finding, not something to hide.
5. **One home for each fact.** Status transitions live only in the State model
   (section 4); routes only in the API surface (section 5); events only in Events
   (section 6); service-to-service routes only in Internal API (section 7); error
   responses only in Errors (section 9). A feature section cites those by rule id,
   method and path, or subject, and does not restate them. Every rule id a table cites
   must exist in the same document.
6. **A rule states one checkable fact.** "Cancel is refused on a finished
   reservation" is a rule. "Cancellation is handled robustly" is not.
7. **Say what happens on failure.** For every call to another service or to a driver,
   state whether the feature fails open or fails closed, and what the caller sees.
8. **Record decisions as decisions.** Where the behavior is a deliberate choice that
   a reader might mistake for a bug, say "by decision" and cite the ADR, document, or
   issue that records it. If nothing records it, it is not a decision yet: ask the
   owner and file an issue.
9. **Defects are not smoothed over.** A rule states what the code does today, even
   when that is wrong. The defect goes under "Open defects" with its GitHub issue, and
   the rule carries a short "Known gap, see #NNN." note. The specification does not
   describe the fix; the issue does.
10. **Plain words first.** Each feature opens with what a user can do, in a sentence a
    non-engineer can follow. Identifiers and code names come after.
11. **Repository style applies.** No em-dashes, no arrows (write "to"), no
    box-drawing characters, no emojis. Tables use markdown table syntax.
12. **File the defect before the document merges.** "Open defects" lists only filed
    issues, and the drift guard rejects a placeholder in place of an issue number. A
    writer reports candidate defects to the maintainer, who verifies and files them;
    the entries and the "Known gap, see #NNN." notes are added before the merge.
13. **Tables cite this document's rules only.** A table in sections 4 to 7 and 9 may
    cite only rule ids defined in the same document, and "Rules with no test" may name
    only this document's unpinned rules. Refer to another area's rule in prose, with
    the document's name.
14. **A partly pinned rule is two rules.** If a test covers one clause of a rule and
    nothing covers another, split the rule so each half can say honestly what pins it.
15. **A test title containing a backtick cannot be cited**, because the reference
    format uses backticks as delimiters. Cite another test in the same file that
    covers the rule, or mark the rule unpinned and name the file in the rule text.

## Keeping a specification current

- A change that alters behavior a specification describes updates that specification
  in the same pull request, the same way it updates `CHANGELOG.md`.
- `tests/unit/test_spec_references.py` is the drift guard. Its module docstring
  defines exactly what counts as a path, a reference line, a rule, and a table
  citation. It fails when a specification:
  - names a repository path that does not exist, or a relative link that does not
    resolve;
  - names an "Enforced in" or "Pinned by" symbol or test name that no longer appears
    in the file it names, or writes a reference line in any other shape;
  - repeats a rule identifier, writes one that does not match `<AREA>-<TOPIC>-<N>`,
    or has a rule without exactly one of each reference line;
  - cites a rule identifier in the State model, API surface, Events, Internal API, or
    Errors table that the document does not define;
  - lists an open defect without an issue, writes an issue reference that is not
    `#` followed by a number, or lets "Rules with no test" disagree with the rules
    marked `Pinned by: none`;
  - leaves out a section heading of the template.
  A withdrawn rule says "withdrawn" in its text and needs no references.
- The header's "Verified at" line names the commit the document was last checked
  against in full. A partial update leaves that line alone.

## Writing a new specification

Copy [`TEMPLATE.md`](TEMPLATE.md), keep every section heading, and write
"None." under a section that does not apply instead of deleting it. A missing section
reads as an oversight; an explicit "None." reads as an answer.
