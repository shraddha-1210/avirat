import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// Build output goes to static/dist so FastAPI can serve the app from the same
// origin in production; the dev server proxies /api to uvicorn instead.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: { outDir: "static/dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
