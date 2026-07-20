/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build emits a self-contained static bundle into ./dist. A later ticket
// wires FastAPI to serve these assets (bundled into the PyPI wheel, Bazarr-style).
// `base: "./"` keeps asset URLs relative so they work under any mount path.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: false,
  },
});
