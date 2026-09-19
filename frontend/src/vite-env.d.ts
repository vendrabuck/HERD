/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_AI_CHAT_ENABLED?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Compile-time constants injected by vite.config.ts's `define` block (issue
// #846). Never referenced directly outside src/lib/appVersion.ts, which
// wraps them; also declared as globals in eslint.config.js so eslint does
// not flag them as undefined.
declare const __APP_VERSION__: string;
declare const __APP_BUILD__: string;
declare const __APP_BUILD_DATE__: string;
