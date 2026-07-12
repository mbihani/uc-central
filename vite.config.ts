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

export default defineConfig({
  root: uiRoot,
  base: "/",
  plugins: [react(), tailwindcss()],
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
