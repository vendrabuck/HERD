/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_AI_CHAT_ENABLED?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
