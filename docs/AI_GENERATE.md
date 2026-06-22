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

The orchestrator constrains the LLM's output via a tool schema built per request. The `template_name` field is restricted to an enum of the templates currently visible to you, so a provider that honors schema enums cannot return a name outside your inventory. The orchestrator also validates the response after the fact and, on a repairable mistake (an unknown template, an over-count, a duplicate role, or an edge to a role that was never defined), re-prompts the model once with the exact allow-list before giving up. Each proposed device has:

- `role` (unique within the proposal; e.g. `fw-a`, `fw-b`, `core-sw-1`)
- `template_name` (must match a real template in your inventory exactly; no invented names)
- `topology_type` (`PHYSICAL` or `CLOUD`, uniform across a single proposal)
- `config` (optional; see [Device configs](#device-configs-the-allowlist))

Edges reference roles by name and carry a `layer` (`L1`, `L2`, or `L3`).

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

Config schemas exist for the `Management`, `Layer 2 Switch`, and `Layer 3 Switch` connection types (the table above shows the Management keys; L2 carries a `vlan_assignments` shape and L3 carries `interfaces`, `virtual_routers`, and `routes`). Layer 1 switches have no schema, so `config` on those devices is rejected at validation. Note that while an L3 config now validates, the post-commit apply step calls the driver's `configure` action, which L3 drivers do not expose (they expose `configure_route`/`remove_route`), so an applied L3 config still fails at execution today. (Automatic VLAN provisioning for L2 switches also happens via the NATS event flow on reservation creation; that path is separate from the AI config allowlist.)

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
4. Creates a reservation.
5. If **Apply device configs** is checked, calls the execution service's `POST /execute` per configured device.

On success, you're navigated to the new topology's page. The toast summarizes anything that went wrong (e.g., "Topology created, but 2 device configs failed to apply").

### What "Apply device configs" actually does

- The execute endpoint requires either admin or a device `manage` grant. If you are not an admin and lack a `manage` grant on a device, that device comes back with `status: failed` and an error of `Admin access or device manage grant required` (or `Admin access required` for a non-`configure` action). The topology and reservation are still created; only the config step fails.
- Per-device failures are recorded in the response as `config_results` but are not persisted in the UI after you navigate away. If you need to see them later, check execution-runs in the execution service's `/runs` endpoint.
- Only L1/L2 fabric wiring is handled by the NATS flow automatically; `configure` on Management devices is the only thing `apply_configs` triggers.

## Rollback behavior

If anything during the commit fails after the topology is created:

- Canvas save fails -> topology deleted, error surfaced.
- Reservation create fails -> topology deleted, error surfaced.
- Config apply fails (per-device) -> **no rollback**; the topology and reservation persist, the failure is recorded in the response.

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
