import { resolve } from "node:path";

import solid from "vite-plugin-solid";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiProxyTarget = env.CHURN_MONITOR_DEV_PROXY_TARGET || "http://127.0.0.1:8000";

  return {
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
      proxy: {
        "/api": {
          target: apiProxyTarget,
          changeOrigin: false,
        },
      },
    },
    build: {
      outDir: resolve("../src/churn_monitor/client"),
      emptyOutDir: true,
    },
  };
});
