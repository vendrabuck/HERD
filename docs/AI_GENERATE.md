# AI Topology Generation

HERD can propose a lab topology from a natural-language prompt by calling the configured LLM provider. The proposal renders as ghost nodes on the canvas for you to inspect before committing. This doc covers the user-facing flow; for the architecture see [ARCHITECTURE.md](ARCHITECTURE.md). For the env-var reference and per-backend setup (vLLM, Ollama, OpenAI, Azure), see [ENV_VARS.md](ENV_VARS.md).

![HERD topology editor with the Use AI entry point (design-system mockup)](img/topology.png)

*The topology editor; the Use AI button opens the prompt dialog, and an accepted proposal renders as reviewable nodes on this canvas. Design-system mockup.*

## Prerequisites

- Your admin must have configured an AI provider. For `AI_PROVIDER=anthropic` that means `AI_API_KEY` is set; for `AI_PROVIDER=openai_compat` that means `AI_BASE_URL` points at a running endpoint. The frontend checks `GET /api/ai/status` on load; when the provider is unconfigured that endpoint reports `{"enabled": false}`, the **Use AI** button is hidden, and `/api/ai/generate` returns 503.
- You need device visibility: the AI can only propose devices your account can see. If your user group has no device group access, the AI will return "no templates available" and refuse.

## The flow, step by step

1. Open the topology editor.
2. Click **Use AI** in the toolbar. A dialog opens with a prompt field and an optional file picker.
3. Write a prompt. Example:
   > Build a high-availability firewall pair with a pair of downstream switches, each switch on its own VLAN.
4. Optionally attach reference files (see [File uploads](#file-uploads) below).
5. Submit. The orchestrator calls the LLM with the current inventory, validates the response, and resolves device ids. You'll see a loading state for a few seconds.
6. The canvas renders the proposal as **ghost nodes** (dashed border, reduced opacity, "PROPOSED" badge). A floating **AI Proposal Bar** appears above the canvas showing the proposal's purpose, device count, edge count, and notes.
7. Pick an action from the proposal bar:
   - **Accept**: commits the ghosts as real nodes and opens the commit dialog for reservation creation.
   - **Modify**: commits the ghosts as real nodes and dismisses the bar so you can freely edit before saving or committing.
   - **Reject**: removes the ghosts and clears the bar; nothing is saved.

## What the LLM proposes

The orchestrator constrains the LLM's output via a tool schema built per request. The `template_name` field is restricted to an enum of the templates currently visible to you, so a provider that honors schema enums cannot return a name outside your inventory. The orchestrator also validates the response after the fact and, on a repairable mistake (an unknown template, an over-count, a duplicate role across devices or elements, an edge to a role that was never defined, an edge connecting two elements directly, an edge connecting a role to itself, or the same device-to-device connection proposed twice), re-prompts the model with the exact allow-list before giving up. The number of re-prompts is `AI_GENERATE_MAX_REPAIRS` (default 2, range 0-5; see [ENV_VARS.md](ENV_VARS.md)); `0` fails the request on the first repairable mistake instead of spending a second provider call. Each proposed device has:

- `role` (unique within the proposal; e.g. `fw-a`, `fw-b`, `core-sw-1`)
- `template_name` (must match a real template in your inventory exactly; no invented names)
- `topology_type` (`PHYSICAL` or `CLOUD`, uniform across a single proposal)
- `config` (optional; see [Device configs](#device-configs-the-allowlist))

Edges reference roles by name and carry a `layer` (`L1`, `L2`, or `L3`).

The LLM may also propose a **network element** (issue #632, ADR 0012 at [`docs/design/0012-network-element-objects.md`](design/0012-network-element-objects.md)): a shared object such as a VLAN segment, subnet, or external cloud that several devices attach to instead of wiring every pair directly. An element has a `role` (from the same namespace as device roles, so role names stay unique across both), an `element_type` (`vlan_segment`, `subnet`, `external_cloud`, or `patch_trunk`), a `label`, and optional descriptive `attrs` (currently `vlan_id`, `cidr`, `description`; not provisioned or otherwise validated). A device attaches to an element with one edge from the device's role to the element's role; the model never names a port for that edge, because the LLM never sees per-device port inventories. Instead, on commit, the orchestrator's committer picks the device's next free port itself (ports sorted in natural name order, e.g. `eth2` before `eth10`, skipping any port another element attachment of the same device already claimed); a device with no free port has that attachment silently dropped rather than committed with a missing port. Element ghost nodes render on the canvas alongside device ghosts and are carried through the same accept/modify/reject flow.

The LLM does **not** propose start/end times for the reservation. Those come from you when you commit.

## File uploads

The AI dialog lets you attach reference files to give the LLM context without having to paste everything into the prompt. Rules:

- Accepted types: `.pdf`, `.txt`, `.md`, `.json`, `.xml`, `.tgz`, `.tar.gz`.
- Max 5 files per request.
- Max 5 MB per file.
- Aggregate text extracted across all files is capped at 80,000 characters (the per-file `truncated` flag appears in the response if a file hit its limit).

Files are parsed into text server-side (PDF via pdfplumber; tar/gz text members extracted; JSON pretty-printed; text passed through) and framed in the prompt as **untrusted context**: the LLM is told to use them only to inform device selection and config, not to treat them as instructions. You get a `file_summaries` block in the response listing each filename, extracted char count, and truncation flag.

Good use cases for uploads:

- A network design PDF with target topology.
- A config file snippet with VLAN ids / IPs to reference.
- A test plan describing what DUT pairings you need.

## Device configs (the allowlist)

The LLM is allowed to include an optional `config` object per device, meant to be applied via the execution service's `configure` action after commit. **Only a fixed allowlist of keys is accepted**; anything else causes the commit to be rejected before any data is written.

| Key | Type | Notes |
|---|---|---|
| `vlan` | integer 1-4094 | VLAN id |
| `ip` | string | IP address or CIDR |
| `hostname` | string | Device hostname |
| `description` | string | Free-text description |

Config schemas exist for the `Management`, `Layer 2 Switch`, and `Layer 3 Switch` connection types (the table above shows the Management keys; L2 carries a `vlan_assignments` shape and L3 carries `interfaces`, `virtual_routers`, and `routes`). Layer 1 switches have no schema, so `config` on those devices is rejected at validation. Note that the required L3 method set does not include `configure` (it is `configure_route`/`remove_route`), so a post-commit apply against an L3 driver works only when the driver also implements `configure` as an optional extra (the checked-in `drivers/mock_l3` is the worked example). Separately, the `routes` array of an L3 device's latest config version is consumed automatically at reservation provisioning time: the execution service installs those routes via `configure_route` when a reservation starts and removes the pinned set on cancel or completion (see the Layer 3 section of [DRIVERS.md](DRIVERS.md)). (Automatic VLAN provisioning for L2 switches also happens via the NATS event flow on reservation creation; both paths are separate from the AI config allowlist.)

This is deliberate: the allowlist prevents an LLM from surfacing arbitrary kwargs into driver code. The schema registry lives in `services/common/herd_common/device_config.py` (the `config_validator` module in this service is a thin re-export of it). Adding a new allowed key requires a one-line change there.

## The commit dialog

On **Accept**, a modal opens with:

- **Topology name** (defaulted to `AI: <purpose> (<timestamp>)`)
- **Start time** (default: now + 1 hour)
- **End time** (default: now + 5 hours)
- **Purpose** (defaulted to the AI's proposed purpose, editable)
- **Apply device configs** checkbox (only shown if the proposal contains at least one non-empty `config`)

Click **Commit**. The orchestrator:

1. Validates every device's config against the allowlist (fails with 422 here if anything is off; nothing is written).
2. Creates the topology in the cabling service.
3. Saves the canvas data.
4. Checks the saved canvas is actually wireable (see [Commit-time wireability check](#commit-time-wireability-check) below).
5. Creates a reservation.
6. If **Apply device configs** is checked, calls the execution service's `POST /execute` per configured device.

On success, you're navigated to the new topology's page. The toast summarizes anything that went wrong (e.g., "Topology created, but 2 device configs failed to apply").

### Commit-time wireability check

Before creating the reservation, the orchestrator calls cabling's own `POST /topologies/{id}/validate` against the canvas it just saved. This catches an edge the LLM proposed between two devices with no physical cable path between them (a fabric with no matching cable, or two devices on unrelated fabrics) before a reservation is ever created, rather than only surfacing it later as reservations' own generic connectivity check. A failure here shows "Commit failed: cannot wire this topology" followed by one line per bad edge, named by role (e.g. `fw-a to sw-a: no cable path`), and the topology is deleted (see [Rollback behavior](#rollback-behavior)). If cabling cannot answer the question at all (an outage, a timeout), the commit fails closed with a 503 rather than proceeding as if the canvas had passed.

### What "Apply device configs" actually does

- The execute endpoint requires either admin or a device `manage` grant. If you are not an admin and lack a `manage` grant on a device, that device comes back with `status: failed` and an error of `Admin access or device manage grant required` (or `Admin access required` for a non-`configure` action). The topology and reservation are still created; only the config step fails.
- Per-device failures are recorded in the response as `config_results` but are not persisted in the UI after you navigate away. If you need to see them later, check execution-runs in the execution service's `/runs` endpoint.
- L1/L2 fabric wiring and L3 static routes are provisioned by the NATS flow automatically on reservation lifecycle events (L3 routes come from the switch's latest config version; see [DRIVERS.md](DRIVERS.md)); `configure` on Management devices is the only thing the `apply_configs` commit step triggers directly.

## Rollback behavior

If anything during the commit fails after the topology is created:

- Canvas save fails: topology deleted, error surfaced.
- Wireability check fails (an unwireable edge, or cabling could not answer): topology deleted, error surfaced.
- Reservation create fails: topology deleted, error surfaced.
- Config apply fails (per-device): **no rollback**; the topology and reservation persist, the failure is recorded in the response.

If the initial topology creation fails, nothing is rolled back because there is nothing to undo.

## Prompt tips

- Name the exact templates you want if you know them (the LLM is forbidden from inventing template names).
- State the count explicitly ("a pair of firewalls", "three test servers"); the LLM won't propose more devices of a template than the available count.
- If your prompt implies VLAN ids or IP addresses, say so; the LLM will add `config` entries within the allowlist. If you don't want configs applied, leave the checkbox off at commit time.
- Attach supporting documents for complex topologies rather than cramming everything into the prompt.

## Known limits and behaviors

- **Inventory shifted during generation (409)**: a device became unavailable between the LLM's proposal and the resolver's fetch. Regenerate.
- **Stale-proposal guards**: the frontend drops any response whose resolved device is null, and any response that references a device already on the canvas.
- **No window generation**: the LLM doesn't pick times; the commit dialog does.
- **Model**: `claude-sonnet-4-6` by default, configurable via `AI_MODEL`. Use Opus if you want higher quality at higher cost; use Haiku for cheaper quick proposals.

## Measuring proposal wireability

A proposal can name valid templates and stay within their available counts and still be
unwireable in practice: `_resolve_devices` (`services/ai-orchestrator/app/services/generator.py`)
assigns the first N AVAILABLE devices per template to each proposed role without consulting the
cabling graph at all, so two devices of the right templates can land on opposite, unconnected
parts of the fabric. Today that only ever surfaces downstream, at reservation-create time, when
cabling's validator finally runs a path check. There was no repeatable way to measure how often
this actually happens, or to tell whether a resolver fix improved it, so `tests/ai_eval/` adds one.

The harness is a scored evaluation, not a product feature: `tests/ai_eval/prompts.json` is a
checked-in set of about ten prompts in plain lab-engineer language (never a template name
verbatim, so the model still has to choose one), each with a stated minimum device and edge
count. `tests/ai_eval/test_generate_eval.py` runs every prompt against a live stack several times,
resolves each proposal into a throwaway topology the same way the committer would (device nodes
and device-to-device edges only; a proposed network element is skipped, since an element edge
never becomes a hop and so never touches pathfinding), calls cabling's validate endpoint, and
scores the result. A run passes when validate reports zero invalid edges AND the proposal met the
prompt's device and edge minimums; a proposal that wires cleanly but only names one device for a
"two firewalls" prompt does not count as a pass. `tests/ai_eval/scoring.py` holds this scoring
logic as pure, stack-free functions and is unit-tested directly in `tests/unit/test_ai_eval_scoring.py`,
which runs in CI with no stack.

This is a measurement tool, not a gate: the suite itself never asserts a pass rate, only that at
least one run completed and a report was written. It is opt-in and needs a running, seeded stack
(`make seed`) with an AI provider configured, so it is not part of `make master`, `make
everything`, or CI. Run it with `make ai-eval`, or directly as `HERD_AI_EVAL=1 uv run pytest
tests/ai_eval/ -v -s`. Env vars:

- `HERD_AI_EVAL` (required, set to `1`): the opt-in switch; the suite skips at module load without it.
- `HERD_AI_EVAL_N` (default `3`): how many times to repeat each prompt.
- `HERD_AI_EVAL_OUT` (default `ai-eval-results.json` in the current directory): where the full set
  of per-run records plus the summary (pass rate, invalid-edge reason counts, latency percentiles)
  is written.

Every throwaway topology the suite creates is named with an `ai-eval-` prefix and deleted in a
`finally` block after validation, so a leftover from an interrupted run is easy to spot and clean
up by name. Compare a `HERD_AI_EVAL_OUT` report from before and after a resolver change to see
whether the fix actually moved the pass rate.
