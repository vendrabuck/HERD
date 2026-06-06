// Build-time feature flags read from Vite env. Add new flags here as
// constants so usage sites do not pepper `import.meta.env` directly. Set in
// frontend/.env (or .env.local for dev overrides) and at the docker compose
// build args for production.

// Branch 3: chat-style assistant UI. When false the legacy single-shot UI
// renders instead; flip on per environment after smoke-testing.
export const AI_CHAT_ENABLED: boolean =
  (import.meta.env.VITE_AI_CHAT_ENABLED ?? "false").toString().toLowerCase() === "true";
