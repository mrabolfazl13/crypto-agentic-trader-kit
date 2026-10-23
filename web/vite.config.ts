import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev-time proxy so the app can call the local API with relative `/api/v1`
// paths. The API is localhost-only, unauthenticated and paper/research-only.
const API_TARGET = "http://127.0.0.1:8177";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
