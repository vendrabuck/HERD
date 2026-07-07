# Decision: AI-Assisted Service-Recipe Authoring, Issue #28

Status: Accepted 2026-07-07. The three product-level decision points below
were resolved with the owner on 2026-07-07 and the engineering defaults were
accepted in review. No code in this doc. Context verified against the live
tree on 2026-07-07 (main at the fleet-utilization merge).

## Context

Issue #32 shipped dynamic resources: a service recipe is an ordinary driver
package (`driver.py` with a `Driver` class, optional `driver_metadata.json`,
optional `config_schema()` classmethod) whose connection type is
`Hypervisor`, with `REQUIRED_METHODS["Hypervisor"] = [login, logout,
create_instance, destroy_instance, status]` (ADR 0004). Authoring one takes
driver-contract knowledge most lab admins do not have, which makes recipes
the bottleneck for onboarding new dynamic-resource kinds. Issue #28 asks the
AI to draft recipes for an administrator to review and approve.

Relevant existing fabric, verified:

- Artifact-drafting precedent: the topology generate/commit flow. `POST
  /generate` forces a single structured tool_use against a per-request JSON
  schema constrained to live inventory, and a separate explicit `POST
  /commit` materializes the reviewed proposal
  (`services/ai-orchestrator/app/services/ai_client.py`,
  `app/routes/generate.py`, `app/routes/commit.py`). AI drafts, validation
  constrains, a human commits. Recipe authoring is the same shape with a
  package instead of a topology.
- Gating precedent: assistant write tools are appended to the advertised
  tool list only when `AI_WRITE_TOOLS_ENABLED` is set AND refused at the
  dispatch boundary independently of the advertised list
  (`app/services/tools.py`, the issue #113 discipline). Gated endpoints
  return 503 with a pinned detail when `ai_is_configured()` is false.
- Upload path: `POST /drivers` and `PUT /drivers/{id}/file` are admin-JWT
  only, accept `.zip`/`.tar.gz`, and perform no structural validation of
  package contents (`services/inventory/app/routers/drivers.py`,
  `app/services/driver_service.py`). All structural truth lives at load time
  in the execution service: `validate_driver` checks `driver.py`, the
  `Driver` class, and `REQUIRED_METHODS` per connection type;
  `extract_config_schema_json` reads `config_schema()` in the sandbox via
  the `__config_schema__` sentinel without instantiating the driver, fail
  open to None (`services/execution/app/services/driver_loader.py`,
  `driver_sandbox.py`).
- Simulation capability: `execute_driver_method` runs any driver method in
  an rlimit-capped subprocess with context via temp file and password keys
  stripped from the child environment. A dry run injects
  `context["dry_run"] = True` and is refused (`DryRunRefused`) unless the
  package metadata declares `supports_dry_run: true`
  (`driver_sandbox.py`). This is the existing run-in-simulation path the AI
  `schedule_config_apply` tool already uses.
- Reference packages: `drivers/mock_hypervisor/` (the Hypervisor contract
  worked example, dry-run honored on every method, idempotent
  `destroy_instance`, `create_instance` returning `{success, instance_ref,
  field_data}`) and `drivers/frr_mgmt/` (the published `config_schema()`
  example). `docs/DRIVERS.md` documents the full package contract including
  the Hypervisor section, the security rules, and the packaging quickstart.
- The sandbox is resource caps, not OS isolation; driver packages are
  trusted code by policy, and the trust decision today is the admin-gated
  upload.

## Decision

### The AI drafts a freeform package; the pipeline, not the format, carries the trust

The LLM drafts a complete recipe package (a `driver.py` and a
`driver_metadata.json`) against the documented contract, grounded on the
reference packages. A constrained skeleton would not reduce blast radius
(arbitrary code inside a method body has the same reach as arbitrary code
anywhere) and a declarative DSL is a separate engineering program whose
step vocabulary would converge back toward a language. Safety comes from
three enforced properties instead:

1. Generated code never executes outside the validation sandbox until an
   admin approves and uploads it.
2. The authoring flow cannot upload. Only the existing admin-JWT `POST
   /drivers` endpoint creates a driver, clicked by a human.
3. AI-drafted recipes must satisfy a stricter contract than hand-written
   ones (below), so they are more validated at review time, not less.

### The generated-recipe contract is stricter than the hand-written one

A draft does not validate unless:

- `driver_metadata.json` declares `supports_dry_run: true`, and every
  mutating method honors `context["dry_run"]` (simulate and return, flag
  results as simulated, no wire I/O).
- `destroy_instance` is idempotent per the ADR 0004 contract.
- The package is standard-library only in v1: no `_deps/` vendoring, no
  imports outside the stdlib. The validator rejects non-stdlib imports.
  Hand-written packages keep the documented `_deps/` path; relaxing this
  for generated packages is future work.
- Secrets discipline: credentials come only from the context (the
  `password_keys` mechanism); the validator rejects string literals that
  look like inline credentials and the prompt forbids them.
- Provenance: the metadata carries `generated_by` (model id), `draft_id`,
  and `generated_at`, so an uploaded recipe is auditable back to its
  drafting session.

### Validation lives in execution behind a new internal endpoint

A new `POST /internal/validate-package` on the execution service
(X-Internal-Token): body carries the package archive (base64) and the
connection type; the response is a structured report. Steps, in order,
each contributing a section to the report:

1. Extract to a temp dir (never into the driver cache; no `DriverCache`
   row, cleaned up afterward).
2. Structural validation: reuse `validate_driver` (class present, required
   methods present, module imports).
3. Static policy checks for the stricter contract above (stdlib-only
   imports, metadata fields, dry-run declaration).
4. Schema extraction via the existing `__config_schema__` sentinel.
5. Sandboxed dry-run: execute `login`, `create_instance`,
   `destroy_instance`, `status`, `logout` with `dry_run: true` and a
   synthetic context (fake endpoint, fake credentials under
   `password_keys`, representative `HERD_<field>` parameters), capturing
   per-method results and transcripts. Refusal or crash is a validation
   failure, not a 500.

Execution owns this endpoint because it owns `REQUIRED_METHODS`, the
sandbox, and the loader; duplicating those checks in ai-orchestrator would
split the source of truth across a service boundary. The endpoint is also
independently useful later (for example an optional validate-on-upload in
inventory), but no other caller is wired in this issue.

### Drafting lives in ai-orchestrator with a bounded auto-repair loop

New admin-only endpoints on the ai-orchestrator service, both gated by
`require_admin`, `ai_is_configured()` (503, pinned detail), and the new
flag below:

- `POST /recipes/draft`: body `{prompt, hypervisor_type?}`. The service
  builds a drafting system prompt embedding the Hypervisor contract and
  security rules from `docs/DRIVERS.md` plus the `mock_hypervisor`
  reference source, forces a structured tool_use whose schema yields
  `{driver_py, driver_metadata, explanation}`, then runs the validation
  loop: assemble the archive, call execution's validate-package, and on a
  red report feed the report back to the model for a bounded number of
  repair attempts (engineering default 3). The final draft is returned and
  persisted regardless of color; a red report is presentable and the admin
  sees exactly what failed.
- `POST /recipes/draft/{draft_id}/refine`: body `{feedback}`. Re-enters
  the same loop seeded with the stored draft plus the admin's feedback.
- `GET /recipes/draft/{draft_id}`: fetch for review, including the
  assembled archive (base64) ready to submit to the existing upload
  endpoint, the validation report, and the dry-run transcripts.

Persistence: a new `recipe_drafts` table in the `ai_orchestrator` schema
(id, user_id, prompt, files JSONB, validation report JSONB, status DRAFT or
UPLOADED-marker-free, model, token usage columns or the existing `ai_usage`
join, timestamps). Drafts are admin-scoped working artifacts, not
reservation conversations, so they do not reuse the assistant conversation
tables. Token usage meters through `ai_usage` exactly as the other AI
features do.

### Gating: a dedicated default-off flag, enforced at the boundary

New env flag `AI_RECIPE_AUTHORING_ENABLED`, default false, mirroring
`AI_WRITE_TOOLS_ENABLED`: the recipe endpoints 403 with a pinned detail
when the flag is off (enforcement in the route dependency, not just
absence from docs), and the unauthenticated `/api/ai/status` response
gains `recipe_authoring: bool` so the UI renders conditionally. Rationale:
this feature asks an LLM to write code that will run against lab
infrastructure; an operator must opt in deliberately, per deployment.

### V1 surface: API plus a minimal review UI on the drivers page

Review is the product; an authoring flow without a place to read the code
undercuts the human-in-the-loop premise. The admin `DriversPage` gains a
"Draft with AI" panel (rendered only when `/api/ai/status` reports
`recipe_authoring: true`): prompt input, code view of `driver.py` and the
metadata, the validation report with per-step pass/fail and the dry-run
transcripts, a refine box, and an "Approve and upload" action that submits
the returned archive through the existing `POST /drivers` multipart call
under the admin's own JWT with the connection type pinned to Hypervisor.
The ai-orchestrator never calls inventory's upload endpoint.

## Decision points

Resolved with the owner 2026-07-07:

1. Artifact form: freeform package (chosen) versus constrained skeleton
   versus declarative DSL.
2. Validation depth: static plus mandatory sandboxed dry-run plus bounded
   auto-repair (chosen) versus static-only versus adding a pre-upload live
   test against a real hypervisor.
3. V1 surface and gating: API plus minimal review UI on DriversPage behind
   a dedicated default-off flag (chosen) versus API-only versus an
   assistant write tool.

Engineering defaults chosen in this ADR (flag in review if disagreed):

4. Validator placement: execution-internal endpoint (default) versus
   duplicating structural checks inside ai-orchestrator. Execution owns
   the loader, sandbox, and REQUIRED_METHODS.
5. Draft persistence: dedicated `recipe_drafts` table (default) versus
   stateless responses versus reusing assistant conversations. Drafts are
   admin working artifacts with an audit value; conversations are
   reservation-scoped.
6. Stdlib-only generated packages in v1 (default) versus allowing `_deps/`
   vendoring in generated output. Vendoring generated dependency trees is
   a supply-chain decision that deserves its own issue.
7. Repair-loop bound: 3 attempts (default), each attempt metered through
   `ai_usage`.
8. V1 connection type: Hypervisor only (default). The same flow generalizes
   to Management and the switch contracts later; the recipe bottleneck is
   the motivating case.

## Testing

- Unit, ai-orchestrator (SQLite in-memory, mocked LLM and validator):
  flag-off refusal at the boundary with pinned wording; unconfigured 503
  contract on both endpoints; draft persistence round trip; repair loop
  stops at the bound and returns the last red report; provenance fields
  injected; usage metered per attempt; admin-only enforcement.
- Unit, execution: validate-package auth (403 wording); each report
  section shape for a known-good package (`mock_hypervisor` bytes) and for
  targeted broken packages (missing method, non-stdlib import, missing
  dry-run declaration, crash inside dry-run); temp-dir cleanup; no
  `DriverCache` row created.
- Functional: a full draft-validate-refine cycle against a scripted mock
  provider whose first draft fails validation and second passes, asserting
  the loop fed the report back.
- Integration (live stack, no LLM needed for the validator): POST the real
  `mock_hypervisor` archive and a hand-broken variant to
  validate-package through the gateway; assert report shapes and that a
  dry-run transcript is present. AI-driven drafting integration remains
  env-gated like the existing assistant live tests.
- Contract: new snapshots for ai-orchestrator (recipes endpoints, status
  field) and execution (internal validate-package); regenerate in the same
  PR that changes the shape.
- E2E (Selenium): DriversPage panel renders only when the status flag is
  on; the flag-off state hides it (the config-gating pattern from the AI
  chat tests).
- Docs: DRIVERS.md gains the generated-recipe contract section;
  AI_ASSISTANT.md or a new AI_RECIPES.md documents the flow; ENV_VARS.md
  gains the flag; FEATURES.md and PLANNED_FEATURES.md flip at ship.

## Phasing

Three PRs, each independently green; the feature is inert until the last:

1. Execution: `POST /internal/validate-package` with the full report
   pipeline and its unit plus integration tests. No callers yet.
2. AI-orchestrator: flag, `recipe_drafts` migration, draft and refine and
   get endpoints with the repair loop, usage metering, contract snapshot,
   unit and functional tests. API complete, UI absent, flag default off.
3. Frontend: the DriversPage panel, e2e, the docs sweep, and the
   FEATURES/PLANNED_FEATURES status flip.

## Out of scope

- Non-Hypervisor connection types (Management, switch contracts); the flow
  is built to generalize but v1 ships one contract.
- A pre-upload live test against a real hypervisor; post-upload testing
  remains the admin's existing manual step.
- Any auto-upload path, including behind the flag.
- `_deps/` vendoring in generated packages (supply-chain decision, own
  issue).
- Editing or refining an already-uploaded driver package in place; v1
  drafts new packages only.
- A declarative recipe DSL.
