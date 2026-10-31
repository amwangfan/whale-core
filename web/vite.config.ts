import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 开发时把 /api 代理到本机 whale-core 管理 API；生产构建由 API 服务直接托管 dist。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
