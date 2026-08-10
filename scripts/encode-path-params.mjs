#!/usr/bin/env node
/**
 * Post-generation codemod: percent-encode dynamic PATH segments in the
 * apx-generated API client (src/permissions_app/ui/lib/api.ts).
 *
 * WHY THIS EXISTS / WHY IT IS DURABLE
 * -----------------------------------
 * `apx build` regenerates api.ts from the FastAPI OpenAPI on every build. The
 * generator (apx 0.2.6, a compiled binary — `_core.abi3.so`) inlines each call
 * as `fetch(`/api/.../${params.x}`)` with the path params interpolated RAW: no
 * `encodeURIComponent`. There is no generator template/config to change, and the
 * generated client has no shared base-request function to hook, so a hand-edit
 * to api.ts would simply be overwritten by the next `apx build`.
 *
 * This codemod runs AFTER generation (wired into `npm run gen`) and rewrites
 * every PATH-portion `${expr}` inside an API URL template literal into
 * `${encodeURIComponent(String(expr))}`. Re-applying it after each regeneration
 * is what makes the fix survive `apx build`.
 *
 * WHY AST, NOT REGEX
 * ------------------
 * The transform is driven by the TypeScript compiler API (already a dependency),
 * which makes it robust where a regex is not:
 *   1. PATH vs QUERY — only substitutions BEFORE the first `?` are encoded;
 *      substitutions in the query string are left as-is (path-encoding a query
 *      value is wrong).
 *   2. SHAPE-TOLERANT — it keys off the template literal itself (any literal
 *      whose static head starts with `/api`), so it handles the inline
 *      `fetch(`…`)` shape, whitespace/newlines (`fetch( `…` )`), AND an
 *      intermediate URL variable (`const url = `…`; fetch(url)`) identically.
 *   3. NESTED BRACES — `${expr}` boundaries come from the real parsed
 *      expression node, so `${fn({a: b})}` is extracted and wrapped correctly,
 *      never truncated/corrupted.
 *   4. GUARD — `findRawPathSegments()` re-parses with the SAME AST logic and
 *      fails the build on ANY genuinely un-encoded path segment. It also fails
 *      on a syntax error (so a corrupted rewrite can never sneak through) and is
 *      not fooled by a string that merely starts with "encodeURIComponent(":
 *      "encoded" means a real CallExpression whose callee is `encodeURIComponent`.
 *
 * WHY HERE AND NOT IN A FETCH WRAPPER / INTERCEPTOR
 * -------------------------------------------------
 * Encoding must happen per-segment BEFORE the URL string is assembled. A fetch
 * wrapper or auth-interceptor only sees the already-joined URL, where an id's
 * "/" is indistinguishable from a real route separator — it cannot correctly
 * re-encode individual segments.
 *
 * Idempotent: a substitution already wrapped in a `encodeURIComponent(...)` call
 * is left alone, so running it twice (or after a no-op regeneration) is a no-op.
 */
import ts from "typescript";
import { readFileSync, writeFileSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const API_HEAD_PREFIX = "/api";

/** A template literal is an API URL if its static head starts with `/api`. */
function isApiUrlTemplate(node) {
  return ts.isTemplateExpression(node) && node.head.text.startsWith(API_HEAD_PREFIX);
}

/** True if `expr` is already a `encodeURIComponent(...)` call (idempotency). */
function isAlreadyEncoded(expr) {
  return (
    ts.isCallExpression(expr) &&
    ts.isIdentifier(expr.expression) &&
    expr.expression.text === "encodeURIComponent"
  );
}

/**
 * Classify every substitution of an API URL TemplateExpression as path or query
 * (query = any `?` appeared in the static text BEFORE this substitution).
 * Returns [{ expr, inPath }] in source order.
 */
function classifySubstitutions(node) {
  const out = [];
  let precedingStatic = node.head.text; // static text before the current span's expr
  for (const span of node.templateSpans) {
    const inPath = !precedingStatic.includes("?");
    out.push({ expr: span.expression, inPath });
    precedingStatic += span.literal.text; // extend for the next span
  }
  return out;
}

function parse(sourceText) {
  return ts.createSourceFile(
    "api.ts",
    sourceText,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    ts.ScriptKind.TS,
  );
}

function eachApiTemplate(sf, visit) {
  const walk = (n) => {
    if (isApiUrlTemplate(n)) visit(n);
    ts.forEachChild(n, walk);
  };
  walk(sf);
}

/**
 * Encode path-portion substitutions in every API URL template literal.
 * Returns { code, wrapped, alreadyWrapped }.
 */
export function encodePathParams(sourceText) {
  const sf = parse(sourceText);
  const edits = []; // { start, end, replacement }
  let wrapped = 0;
  let alreadyWrapped = 0;

  eachApiTemplate(sf, (node) => {
    for (const { expr, inPath } of classifySubstitutions(node)) {
      if (!inPath) continue; // query substitutions are NOT path-encoded
      if (isAlreadyEncoded(expr)) {
        alreadyWrapped += 1;
        continue; // idempotent
      }
      const exprText = expr.getText(sf); // real boundaries incl. nested braces
      edits.push({
        start: expr.getStart(sf),
        end: expr.getEnd(),
        replacement: `encodeURIComponent(String(${exprText}))`,
      });
      wrapped += 1;
    }
  });

  // Apply edits right-to-left so earlier offsets stay valid.
  edits.sort((a, b) => b.start - a.start);
  let code = sourceText;
  for (const e of edits) {
    code = code.slice(0, e.start) + e.replacement + code.slice(e.end);
  }
  return { code, wrapped, alreadyWrapped };
}

/**
 * GUARD: return the list of genuinely-raw path substitutions in `sourceText`
 * (empty when every API path segment is properly encoded). Throws if the text
 * does not parse, so a corrupted rewrite can never pass as "encoded".
 */
export function findRawPathSegments(sourceText) {
  const sf = parse(sourceText);
  if (sf.parseDiagnostics && sf.parseDiagnostics.length > 0) {
    const msgs = sf.parseDiagnostics
      .map((d) => ts.flattenDiagnosticMessageText(d.messageText, "\n"))
      .join("; ");
    throw new Error(`api.ts failed to parse after transform: ${msgs}`);
  }
  const leaks = [];
  eachApiTemplate(sf, (node) => {
    for (const { expr, inPath } of classifySubstitutions(node)) {
      if (inPath && !isAlreadyEncoded(expr)) leaks.push(expr.getText(sf));
    }
  });
  return leaks;
}

function runCli() {
  const here = dirname(fileURLToPath(import.meta.url));
  const apiPath = resolve(here, "../src/permissions_app/ui/lib/api.ts");

  let src;
  try {
    src = readFileSync(apiPath, "utf8");
  } catch (e) {
    console.error(`[encode-path-params] cannot read ${apiPath}: ${e.message}`);
    process.exit(1);
  }

  const { code, wrapped, alreadyWrapped } = encodePathParams(src);
  if (code !== src) writeFileSync(apiPath, code, "utf8");

  console.log(
    `[encode-path-params] api.ts: wrapped ${wrapped} path interpolation(s), ` +
      `${alreadyWrapped} already encoded.`,
  );

  let leaks;
  try {
    leaks = findRawPathSegments(code);
  } catch (e) {
    console.error(`[encode-path-params] GUARD FAILED: ${e.message}`);
    process.exit(1);
  }
  if (leaks.length) {
    console.error(
      `[encode-path-params] GUARD FAILED: ${leaks.length} un-encoded path ` +
        `segment(s) remain: ${leaks.join(", ")}`,
    );
    process.exit(1);
  }
}

// Run only when invoked directly (not when imported by the test harness).
const invokedDirectly =
  process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) runCli();
