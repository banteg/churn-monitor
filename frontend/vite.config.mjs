import { resolve } from "node:path";

import solid from "vite-plugin-solid";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [solid()],
  resolve: {
    alias: {
      // The package's default export is compiled out in production builds,
      // but this app serves a production bundle locally and still needs the panel.
      "@tanstack/solid-query-devtools": resolve(
        "node_modules/@tanstack/solid-query-devtools/build/dev.js",
      ),
    },
  },
  server: {
    fs: {
      allow: [resolve("."), resolve("..")],
    },
  },
  build: {
    outDir: resolve("../src/churn_monitor/client"),
    emptyOutDir: true,
  },
});
