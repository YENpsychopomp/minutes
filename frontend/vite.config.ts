import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端只吃 /api/*；開發時由 Vite 反向代理到本機 Python 服務（summery serve）。
const API_TARGET = process.env.SUMMERY_API_ORIGIN ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    cssCodeSplit: false,
  },
  server: {
    host: "127.0.0.1",
    port: Number(process.env.VITE_PORT ?? 5173),
    strictPort: false,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: false },
    },
  },
  preview: { host: "127.0.0.1", port: Number(process.env.VITE_PREVIEW_PORT ?? 4173) },
});
