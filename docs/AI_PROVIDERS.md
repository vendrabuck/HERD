# Configuring HERD's AI provider

HERD's AI features (`/api/ai/generate`, `/api/ai/templates/suggest-identity`, `/api/ai/reservations/{id}/assistant`) are powered by a pluggable LLM provider. You select the backend with `AI_PROVIDER` and HERD adapts: `anthropic` calls the Anthropic API; `openai_compat` calls any OpenAI-compatible chat-completions endpoint, including vLLM, Ollama, LM Studio, OpenAI proper, and Azure OpenAI.

This document covers the operational steps to switch providers on a running deployment. For the env-var reference, see [ENV_VARS.md](ENV_VARS.md). For the user-facing flows, see [AI_GENERATE.md](AI_GENERATE.md) and [AI_ASSISTANT.md](AI_ASSISTANT.md).

## What changes when you switch

Only two services consume the AI environment variables: `ai-orchestrator` (the service that calls the LLM) and `config` (the service that surfaces these settings in the in-app config editor). Other services do not care about the provider. That means switching requires recreating those two containers, not the whole stack.

## Before you switch

- Confirm the destination provider works in isolation. If you are pointing at a local LLM via `openai_compat`, the endpoint should answer `GET /v1/models` with a JSON list. For a keyless local `anthropic` endpoint, `AI_BASE_URL` is the server root and it should serve `POST /v1/messages` in Anthropic format. For hosted Anthropic, the key should be valid.
- If you are pointing at a self-signed local LLM, set `AI_TLS_VERIFY=false`. Without it, the AsyncOpenAI SDK will refuse the connection at TLS handshake.
- If you are using tool-use (which `/generate` and the reservation assistant both require), confirm the local server is launched with tool-call parsing enabled. For vLLM that is `--enable-auto-tool-choice` plus a `--tool-call-parser` flag appropriate for the model family. Without it the loop exits early with raw text instead of structured tool calls.

## Step-by-step: switch to a local OpenAI-compatible endpoint

1. Edit `.env`:

   ```env
   AI_PROVIDER=openai_compat
   AI_BASE_URL=https://your-llm-host:8000/v1
   AI_API_KEY=any-non-empty-placeholder-or-real-key
   AI_MODEL=your-model-identifier
   AI_TLS_VERIFY=false
   ```

   `AI_API_KEY` must be non-empty even for local servers that ignore auth (the AsyncOpenAI SDK rejects blank keys; HERD substitutes the literal `EMPTY` when this is blank for the `openai_compat` provider). `AI_MODEL` is the exact identifier the local server expects: for vLLM that is the HuggingFace path or the alias you launched the server with; for Ollama that is the model tag.

2. Recreate the two services that read the AI variables:

   ```bash
   docker compose up -d --force-recreate ai-orchestrator config
   ```

   `docker compose restart` does NOT re-read `.env`; you must `up -d --force-recreate` for the env change to take effect.

3. Verify the orchestrator reports the new provider:

   ```bash
   curl -sk https://localhost/api/ai/status
   ```

   Expected:

   ```json
   {"enabled": true, "provider": "openai_compat", "model": "your-model-identifier", "recipe_authoring": false, "degraded": false, "reason": null, "purpose_classification": false}
   ```

4. Make a real call to confirm end-to-end wiring. The cheapest one is `suggest-identity`; it is a single round trip and exercises tool-use:

   ```bash
   TOKEN=$(curl -sk -X POST https://localhost/api/auth/login \
     -H "Content-Type: application/json" \
     -d '{"email":"<admin>","password":"<password>"}' | jq -r .access_token)

   curl -sk -X POST https://localhost/api/ai/templates/suggest-identity \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"FW-400 Firewall"}'
   ```

   A clean response looks like `{"vendor":"...","model":"...","confidence":"high","reasoning":"..."}`. If you get back HTML, your stack URL is wrong (Traefik fell through to the SPA catch-all). If you get back a 503, the provider thinks it is unconfigured: re-check `AI_PROVIDER` and `AI_BASE_URL` on the running container with `docker compose exec ai-orchestrator env | grep AI_`.

## Step-by-step: switch to the Anthropic API

1. Edit `.env`:

   ```env
   AI_PROVIDER=anthropic
   AI_BASE_URL=
   AI_API_KEY=sk-ant-...
   AI_MODEL=claude-sonnet-4-6
   AI_TLS_VERIFY=true
   ```

   `AI_BASE_URL` is honored under the `anthropic` provider: when set, the SDK targets that URL instead of the hosted API (this is the keyless local-endpoint mode), and a non-empty `AI_BASE_URL` alone keeps AI enabled even with a blank key. Clear it when switching to the hosted API. `AI_TLS_VERIFY=true` is the default; explicit is fine.

2. Same recreate command:

   ```bash
   docker compose up -d --force-recreate ai-orchestrator config
   ```

3. Same status check:

   ```bash
   curl -sk https://localhost/api/ai/status
   ```

   Expected:

   ```json
   {"enabled": true, "provider": "anthropic", "model": "claude-sonnet-4-6", "recipe_authoring": false, "degraded": false, "reason": null, "purpose_classification": false}
   ```

4. Same smoke call (`suggest-identity`).

## Disabling AI entirely

To disable AI without removing the configuration, blank the credential for the active provider:

- Under `anthropic`: set both `AI_API_KEY=` and `AI_BASE_URL=` (empty); either one alone keeps the provider configured and AI enabled.
- Under `openai_compat`: set `AI_BASE_URL=` (empty).

Recreate `ai-orchestrator` and `config`. `GET /api/ai/status` will then return `{"enabled": false, "provider": "...", "model": "...", "recipe_authoring": ..., "degraded": false, "reason": null, "purpose_classification": false}`, the frontend will hide the **Use AI** button, the **AI Assistant** tab, and the **Draft with AI** panel, and the guarded endpoints will return 503.

## Choosing a model

The provider you pick determines the model identifier format, but the choice of model size matters across all providers:

- **Topology generation (`/api/ai/generate`)**: a single tool-use turn against the inventory. Mid-size models in the 14B-class can serve this well.
- **Template identity (`/api/ai/templates/suggest-identity`)**: a single tool-use turn, narrow output. Small models in the 7B-class can serve this.
- **Reservation assistant (`/api/ai/reservations/{id}/assistant`)**: multi-turn tool-use loop, the hardest surface. Realistic floor is the 30B+ class (Qwen 3 family, Llama 3.3 70B, etc.). Smaller models tend to get confused after two or three tool calls.

For local deployments you can configure two HERD instances pointing at two different model sizes, or accept that the assistant tab will degrade gracefully when the model is too small.

## Common backends and example settings

### vLLM

```env
AI_PROVIDER=openai_compat
AI_BASE_URL=http://vllm:8000/v1
AI_API_KEY=EMPTY
AI_MODEL=Qwen/Qwen3-35B-Instruct
AI_TLS_VERIFY=true
```

Launch vLLM with `--enable-auto-tool-choice --tool-call-parser <parser>` where `<parser>` matches the model family (Hermes-style works for Qwen3; some releases ship a model-specific parser). The base URL must include `/v1`.

If your vLLM serves HTTPS with a self-signed cert (common on bench deployments), set `AI_TLS_VERIFY=false`.

vLLM quirk (watch for this): under some configurations vLLM returns `finish_reason="stop"` even when tool calls are present. The OpenAI SDK maps that to `stop_reason="end_turn"`, and HERD's agentic loop continues only while `stop_reason == "tool_use"` and tool-use blocks are present (it exits when either is false). So if your vLLM build exhibits this quirk, the tool calls in that turn are not executed and the loop ends early. If AI features behave as though tools never ran, check whether the server is returning `finish_reason="stop"` alongside tool calls and adjust the vLLM tool-call / guided-decoding settings so it reports the tool-call finish reason.

### Ollama

```env
AI_PROVIDER=openai_compat
AI_BASE_URL=http://ollama:11434/v1
AI_API_KEY=EMPTY
AI_MODEL=qwen2.5:32b
AI_TLS_VERIFY=true
```

Ollama added an OpenAI-compatible surface in 0.1.30. Plain HTTP by default; `AI_TLS_VERIFY` only matters if you front it with a self-signed proxy.

### LM Studio

```env
AI_PROVIDER=openai_compat
AI_BASE_URL=http://lm-studio:1234/v1
AI_API_KEY=EMPTY
AI_MODEL=lmstudio-community/Qwen2.5-32B-Instruct-GGUF
AI_TLS_VERIFY=true
```

LM Studio's OpenAI surface is on by default in the **Local Server** tab.

### OpenAI proper

```env
AI_PROVIDER=openai_compat
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=sk-...
AI_MODEL=gpt-4o-mini
AI_TLS_VERIFY=true
```

`AI_BASE_URL` must be set explicitly: HERD treats `openai_compat` as unconfigured when it is blank (`/api/ai/status` reports `enabled: false` and the gated endpoints return 503), so the SDK's default URL is never reached.

### Azure OpenAI

Azure uses a non-standard URL shape (deployment-specific). Set `AI_BASE_URL` to the full path including the deployment, e.g. `https://<resource>.openai.azure.com/openai/deployments/<deployment>`. The Azure auth header is also non-standard (`api-key:`), which is currently NOT supported by HERD's OpenAICompatProvider. Azure will land properly when a dedicated provider class is added; for now it is best-effort.

### Anthropic

```env
AI_PROVIDER=anthropic
AI_BASE_URL=
AI_API_KEY=sk-ant-...
AI_MODEL=claude-sonnet-4-6
AI_TLS_VERIFY=true
```

`AI_TLS_VERIFY` (and `AI_CA_CERT`) are honored by both providers whenever `AI_BASE_URL` is set: each builds its HTTP client with the configured TLS verification and CA bundle and hands it to the SDK, including the keyless local `anthropic` endpoint.

## Troubleshooting

- **`/api/ai/status` returns `{"enabled": false}` after the switch**: the orchestrator does not see the new config. Check `docker compose exec ai-orchestrator env | grep AI_` to confirm the env actually reached the container. If the env is correct but the status is wrong, the container is still the pre-edit one; rerun `docker compose up -d --force-recreate ai-orchestrator`.
- **`/api/ai/status` returns `{"enabled": false, "degraded": true, "reason": "..."}`**: settings look sufficient but the provider failed to CONSTRUCT (issue #606), for example an `AI_CA_CERT` path that does not exist, or an SDK/http-client version mismatch. `reason` is the exception class name only (never the message, which can carry a base URL or key material); check the ai-orchestrator container logs for the full `ai_status_construction_probe_failed` or `ai_client_construction_failed` line. The result is cached for 30 seconds, so a config fix needs up to that long (or a container recreate, which resets the cache) to be reflected.

- **`/api/ai/status` returns the HTML SPA index**: you hit the Traefik catch-all. Either the URL is wrong (the path was not `/api/ai/status` exactly) or the ai-orchestrator container is not healthy yet (Traefik falls back to the SPA when the labelled service has no healthy endpoints). Wait a few seconds and retry, or `docker compose ps ai-orchestrator` to confirm health.

- **TLS handshake error from a local self-signed LLM**: set `AI_TLS_VERIFY=false`. The error in `ai-orchestrator` logs will mention `SSLCertVerificationError` or `CERTIFICATE_VERIFY_FAILED`.

- **Tool calls do not happen, loop returns raw text**: the local server is not configured for tool-use. For vLLM, this means missing `--enable-auto-tool-choice` or a wrong `--tool-call-parser`. Fix the server config, not HERD.

- **AI features work but responses are obviously low quality**: the model is too small for the surface. Try a larger model, especially for the reservation assistant which is the most demanding.

- **`/api/ai/generate` returns "AI referenced unknown templates"**: the model proposed a template name that does not exist in your inventory. Either the stack is unseeded (`make seed` to populate; see [seed_devices_public.py](../seed_devices_public.py)) or your user account lacks visibility into the required device groups.
