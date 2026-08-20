import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { parse as parseTOML } from "smol-toml";
import { defineConfig } from "vite";

// Materialized apx-managed vite config so `vite build` can run standalone.
//
// `root` points at the UI source ([tool.apx.ui] root in pyproject.toml) and the
// bundle is emitted into the Python package's __dist__ folder, which the wheel
// ships (see [tool.hatch.build] artifacts) and FastAPI mounts at "/".
const uiRoot = resolve(__dirname, "src/permissions_app/ui");
const distDir = resolve(__dirname, "src/permissions_app/__dist__");

// The displayed brand (e.g. "UC Central") has exactly ONE source:
// pyproject `[tool.apx.metadata] app-name`. apx regenerates
// `src/permissions_app/_metadata.py` `app_name` from it on every `apx build`, and
// we read it here so the navbar/sidebar text (logo.tsx), the HTML <title> (via
// the transformIndexHtml plugin below), and the FastAPI/OpenAPI title (app.py)
// all stay in sync. A customer renames the product in one place.
//
// The deploy identity is intentionally decoupled: the `databricks.yml`
// bundle/app name, the Python package dir/slug (`permissions_app`), and the
// `PERMISSIONS_APP_*` env prefix all stay as-is, so a brand rename never
// disturbs the deployment. See MANIFESTO.md for the full rationale.
//
// There is intentionally NO literal fallback: if `app-name` cannot be read from
// pyproject.toml the build FAILS (throws) rather than silently using a stale
// hardcoded string — guaranteeing the single source of truth is authoritative.
function readAppName(): string {
  const pyprojectPath = resolve(__dirname, "pyproject.toml");
  const pyprojectText = readFileSync(pyprojectPath, "utf-8");
  const pyproject = parseTOML(pyprojectText);
  const appName = (pyproject.tool?.apx?.metadata as { "app-name"?: unknown })
    ?.["app-name"];
  if (typeof appName !== "string" || appName.length === 0) {
    throw new Error(
      `vite build: cannot read [tool.apx.metadata] app-name from ${pyprojectPath}. ` +
        `This is the single source of the product brand; set it to a non-empty string.`,
    );
  }
  return appName;
}

const appName = readAppName();

/** Minimal HTML-escape so a brand containing &, <, or > can't corrupt the title. */
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export default defineConfig({
  root: uiRoot,
  base: "/",
  plugins: [
    react(),
    tailwindcss(),
    // Inject the brand name into index.html at build time so the <title> (and
    // any other __APP_NAME__ placeholder in the HTML shell) is sourced from the
    // same pyproject `app-name` as the JS __APP_NAME__ define — no second brand
    // source to keep in sync. Escaped so a brand with HTML metacharacters can't
    // break the markup.
    {
      name: "inject-app-name",
      transformIndexHtml(html: string) {
        return html.replace(/__APP_NAME__/g, escapeHtml(appName));
      },
    },
  ],
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
