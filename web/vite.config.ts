import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/limbowave/web/static",
    emptyOutDir: false,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks(id) {
          const path = id.replace(/\\/g, "/");
          // CommonJS helpers and *all* React subpaths are shared by the entry.
          // Assign them explicitly, otherwise Rollup can hoist them into the
          // optional Markdown chunk and silently make that chunk eager again.
          if (
            path.includes("commonjsHelpers") ||
            /\/node_modules\/(react|react-dom|scheduler)\//.test(path)
          )
            return "uiRuntime";
          if (/\/node_modules\/(react-markdown|remark-gfm)\//.test(path))
            return "markdown";
          if (path.includes("/node_modules/zod/")) return "validation";
          if (path.includes("/node_modules/node-forge/"))
            return "passwordCrypto";
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: process.env.LIMBOWAVE_API_ORIGIN || "http://127.0.0.1:8765",
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
