import { resolve } from "node:path";

import solid from "vite-plugin-solid";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [solid()],
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
