import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, proxy gateway paths to a locally running gateway (same-origin, like nginx in Docker).
const GATEWAY = process.env.GATEWAY_URL ?? "http://127.0.0.1:8787";
const proxied = ["/mcp", "/v1", "/admin", "/healthz", "/readyz", "/metrics"];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    fs: { allow: [".."] }, // experiments/results.json lives outside the console root
    proxy: Object.fromEntries(proxied.map((p) => [p, { target: GATEWAY, changeOrigin: false }])),
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 800 },
});
