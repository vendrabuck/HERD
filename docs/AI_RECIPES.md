# AI-Assisted Recipe Authoring

Issue #28; design in [docs/design/0005-ai-recipe-authoring.md](design/0005-ai-recipe-authoring.md).
An administrator describes a dynamic-resource recipe in natural language; the
AI drafts the driver package; the execution sandbox validates it; the admin
reviews, iterates, and explicitly approves the upload. AI drafts, a human
commits: the same shape as topology generation's generate/commit split.

## Enabling it

Dark by default. Three conditions must all hold:

1. An AI provider is configured (`ai_is_configured()`, the same gate as every
   AI feature; see [AI_PROVIDERS.md](AI_PROVIDERS.md)).
2. `AI_RECIPE_AUTHORING_ENABLED=true` on the ai-orchestrator service. The
   drafting endpoints return 403 `AI recipe authoring is disabled` when off;
   enforcement is at the route boundary, mirroring `AI_WRITE_TOOLS_ENABLED`.
3. The caller is an admin or superadmin.

`GET /api/ai/status` reports the flag as `recipe_authoring`; the frontend
renders the "Draft with AI" button on the drivers page only when both
`enabled` and `recipe_authoring` are true. The ai-orchestrator also needs
`INTERNAL_API_TOKEN` (its only internal-token use) to reach the validator.

## The flow

1. `POST /api/ai/recipes/draft` `{prompt, hypervisor_type?}`. The service
   forces a single structured `draft_recipe` tool call; the model returns the
   complete `driver.py`, a small metadata subset (name/version/notes), and an
   explanation for the reviewer.
2. The service assembles the package, injecting the owned metadata fields the
   model is never trusted with: `connection_type: Hypervisor`,
   `supports_dry_run: true`, and provenance (`generated_by`, `draft_id`,
   `generated_at`).
3. The package goes to execution's internal `POST /internal/validate-package`:
   AST structural checks (the unapproved code is never imported in-process),
   the stricter generated-recipe policy (stdlib-only, no inline credentials,
   dry-run declared), config-schema extraction, and a sandboxed dry-run of
   the full lifecycle against a synthetic context on the reserved `.invalid`
   TLD. See the generated-recipes section of [DRIVERS.md](DRIVERS.md).
4. On a red report the errors are flattened and fed back to the model, up to
   `AI_RECIPE_MAX_ATTEMPTS` (default 3) total attempts. The final draft
   persists either way; a red report is presentable and the admin sees
   exactly what failed.
5. The admin reviews in the drivers-page panel: the code, the metadata, the
   validation report, and the per-method dry-run transcripts. Iterate with
   `POST /api/ai/recipes/draft/{id}/refine` `{feedback}`; fetch any draft
   with `GET /api/ai/recipes/draft/{id}`.
6. Approval is the admin's explicit click: the panel submits the returned
   archive through inventory's existing admin `POST /drivers` endpoint under
   the admin's own JWT, with the connection type pinned to Hypervisor. The
   panel refuses to upload a draft that failed validation (download it and
   finish by hand if you want it anyway). The ai-orchestrator itself cannot
   upload a driver.

## Cost and quota

Every drafting attempt (including auto-repair rounds) is metered through the
`ai_usage` table and counts against `AI_DAILY_TOKEN_QUOTA` when a quota is
configured; a caller over quota gets 429 before the provider is called.

## Failure modes

| Symptom | Meaning |
|---|---|
| 403 `AI recipe authoring is disabled` | The flag is off on ai-orchestrator. |
| 503 `AI orchestrator is not configured` | No usable AI provider (same as every AI feature). |
| 503 `AI provider is unreachable` | Provider transport failure. |
| 503 `Recipe validator is unreachable` | Execution's validate-package endpoint could not be reached; a draft is never presented as reviewed when it was not validated. |
| Draft returned with `valid: false` | The repair loop exhausted its attempts; the validation report in the response says exactly what failed. Refine, or download and fix by hand. |

## What v1 deliberately does not do

- Only the Hypervisor connection type (recipes); other driver contracts are
  future work.
- No live test against a real hypervisor before upload; post-upload testing
  remains the admin's existing manual step.
- No auto-upload under any flag combination.
- No `_deps/` vendoring in generated packages (hand-written packages keep it).
