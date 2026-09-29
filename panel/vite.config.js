import { defineConfig } from "vite";
// The built panel is committed, so `git clone && ./install.sh` needs no npm.
// Rebuild with: npm install && npm run build
export default defineConfig({
  base: "/",
  build: { outDir: "dist", emptyOutDir: true, target: "es2020" },
  server: { proxy: { "/api": "http://localhost:8080" } },
});
