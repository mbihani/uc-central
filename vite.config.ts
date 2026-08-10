import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Reconstruction of the apx-managed vite config for offline builds.
// apx normally generates this on the fly; we materialize it so `vite build`
// can run without apx's networked installer.
//
// `root` points at the UI source ([tool.apx.ui] root in pyproject.toml) and the
// bundle is emitted into the Python package's __dist__ folder, which the wheel
// ships (see [tool.hatch.build] artifacts) and FastAPI mounts at "/".
const uiRoot = resolve(__dirname, "src/permissions_app/ui");
const distDir = resolve(__dirname, "src/permissions_app/__dist__");

// apx normally injects a `define` for the `__APP_NAME__` build-time global the
// UI references (see ui/components/apx/logo.tsx). The materialized config must
// reproduce it, otherwise the bundle ships a bare undefined global and the app
// throws a ReferenceError at runtime ("APP_NAME NOT DEFINED"). Read the value
// from the canonical source ([tool.apx.metadata] app-name in pyproject.toml) so
// it stays in sync, with a hard fallback matching src/permissions_app/_metadata.py.
function readAppName(): string {
  const fallback = "permissions-app";
  try {
    const pyproject = readFileSync(resolve(__dirname, "pyproject.toml"), "utf-8");
    const match = pyproject.match(/^\s*app-name\s*=\s*["']([^"']+)["']/m);
    return match?.[1] ?? fallback;
  } catch {
    return fallback;
  }
}

const appName = readAppName();

export default defineConfig({
  root: uiRoot,
  base: "/",
  plugins: [react(), tailwindcss()],
  define: {
    __APP_NAME__: JSON.stringify(appName),
  },
  resolve: {
    alias: {
      "@": uiRoot,
    },
  },
  build: {
    outDir: distDir,
    emptyOutDir: true,
  },
});
