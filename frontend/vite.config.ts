import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// The app lives in frontend/ but builds UP into the repo-root static/dist, which
// is what FastAPI serves — same origin in production, no CORS. `emptyOutDir` is
// explicit because the target sits outside this Vite root and Vite refuses to
// clear such a directory silently.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: { outDir: path.resolve(__dirname, "../static/dist"), emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
