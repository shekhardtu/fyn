import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";

/* The browser talks to the app's own origin and this proxy relays /api to the
   backend, mirroring nginx.conf in production. Same-origin is what keeps the
   httpOnly session cookie first-party — installed-PWA standalone mode and
   third-party-cookie phase-outs both punish the cross-origin alternative. */
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";
const apiProxy = {
  "/api": {
    target: BACKEND_ORIGIN,
    changeOrigin: true,
  },
} as const;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 3000,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: "0.0.0.0",
    port: 3000,
    strictPort: true,
    proxy: apiProxy,
  },
});
