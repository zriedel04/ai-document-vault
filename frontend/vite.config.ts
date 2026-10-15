/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// The launcher (vault.ps1 dev) starts Vite with a fresh random value in VAULT_LAUNCH_ID and opens
// the browser, with the vault's token in the address, only once this path answers with the same
// value. Any program can answer on port 5173, but none can read the environment of the process
// the launcher started, so the value proves that the page the token goes to is this one. The
// backend's /api/health does the same for "run" (vault/api/health.py).
export const LAUNCH_PATH = "/__vault/launch";
const LAUNCH_SHAPE = /^[A-Za-z0-9_-]{32,128}$/;

export function launchId(value: string | undefined): string | null {
  // No "m" flag, and "$" in JavaScript does not match before a trailing newline.
  return value !== undefined && LAUNCH_SHAPE.test(value) ? value : null;
}

function launchCheck(): Plugin {
  return {
    name: "vault-launch-check",
    configureServer(server) {
      const launch = launchId(process.env.VAULT_LAUNCH_ID);
      server.middlewares.use(LAUNCH_PATH, (_request, response) => {
        response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify({ launch }));
      });
    },
  };
}

// In development Vite serves the app and forwards /api to the backend, so the browser sees one
// origin, as it does in normal use when the backend serves the built files itself.
export default defineConfig({
  plugins: [react(), launchCheck()],
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
    css: false,
  },
});
