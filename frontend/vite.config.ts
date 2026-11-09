import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物输出到后端托管的 web/dist（game_agent.main._frontend_dir 会探测该路径）。
// 开发模式下把 /api 与 /ws 代理到本地后端（默认 8765）。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../web/dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8765", changeOrigin: true },
      "/ws": { target: "ws://localhost:8765", ws: true },
    },
  },
});
