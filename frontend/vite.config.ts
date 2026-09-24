import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";
import { readFileSync } from "fs";

// Read straight from package.json rather than hardcoding, so a release bump
// (which touches only the pyprojects and package.json per issue #846) needs
// no edit here. VITE_HERD_BUILD/VITE_HERD_BUILD_DATE are supplied by the
// Docker build args wired elsewhere (issue #846 lane 2); a bare `npm run
// build` outside that plumbing leaves them unset, and both fall back to a
// value that still renders sensibly (see src/lib/appVersion.ts).
const pkg = JSON.parse(readFileSync(path.resolve(__dirname, "package.json"), "utf-8")) as {
  version: string;
};

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
    __APP_BUILD__: JSON.stringify(process.env.VITE_HERD_BUILD || "dev"),
    __APP_BUILD_DATE__: JSON.stringify(process.env.VITE_HERD_BUILD_DATE || ""),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost",
        changeOrigin: true,
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 800,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/test/**",
        "src/**/*.d.ts",
        "src/**/*.types.ts",
        "src/main.tsx",
        "src/vite-env.d.ts",
      ],
      reporter: ["text", "json-summary", "html"],
    },
  },
});
