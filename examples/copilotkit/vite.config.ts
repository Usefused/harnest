import { defineConfig } from "vite";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api/adk": {
        target: "http://127.0.0.1:1910",
        rewrite: (p) => p.replace(/^\/api\/adk/, ""),
      },
      "/api/langgraph": {
        target: "http://127.0.0.1:1911",
        rewrite: (p) => p.replace(/^\/api\/langgraph/, ""),
      },
    },
  },
});
