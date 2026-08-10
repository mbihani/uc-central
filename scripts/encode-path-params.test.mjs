#!/usr/bin/env node
/**
 * Adversarial unit tests for the AST-based path-param codemod.
 * Run: node scripts/encode-path-params.test.mjs   (exit 0 = all pass)
 */
import assert from "node:assert";
import { encodePathParams, findRawPathSegments } from "./encode-path-params.mjs";

let failures = 0;
function test(name, fn) {
  try {
    fn();
    console.log(`  CONFIRMED  ${name}`);
  } catch (e) {
    failures += 1;
    console.error(`  FAILED     ${name}\n             ${e.message}`);
  }
}

// The exact broken regex the previous version used — to prove the new guard
// catches what it would have produced.
function oldBrokenRegex(src) {
  return src.replace(/fetch\(`([^`]*)`/g, (_m, tpl) => {
    const enc = tpl.replace(
      /\$\{([^}]+)\}/g,
      (_w, expr) => "${encodeURIComponent(String(" + expr.trim() + "))}",
    );
    return "fetch(`" + enc + "`";
  });
}

console.log("Adversarial codemod tests:");

// 1. QUERY vs PATH: path substitution encoded, query substitution left as-is.
test("query vs path — only path segment encoded", () => {
  const src = "const res = await fetch(`/api/x/${id}?flag=${opt}`);";
  const { code } = encodePathParams(src);
  assert(
    code.includes("/api/x/${encodeURIComponent(String(id))}?flag=${opt}"),
    `got: ${code}`,
  );
  assert(!code.includes("encodeURIComponent(String(opt))"), "query value must NOT be path-encoded");
  assert.deepStrictEqual(findRawPathSegments(code), [], "no raw path segments remain");
});

// 2. NESTED BRACES: full expression wrapped, not corrupted.
test("nested braces — expression wrapped intact", () => {
  const src = "const res = await fetch(`/api/y/${fn({a: b})}`);";
  const { code } = encodePathParams(src);
  assert(
    code.includes("${encodeURIComponent(String(fn({a: b})))}"),
    `got: ${code}`,
  );
  assert.deepStrictEqual(findRawPathSegments(code), []);
});

// 3a. ALTERNATE SHAPE: whitespace between fetch( and the template.
test("alternate shape — whitespace fetch( ` ` )", () => {
  const src = "const res = await fetch( `/api/z/${id}` );";
  const { code } = encodePathParams(src);
  assert(code.includes("/api/z/${encodeURIComponent(String(id))}"), `got: ${code}`);
  assert.deepStrictEqual(findRawPathSegments(code), []);
});

// 3b. ALTERNATE SHAPE: intermediate URL variable, then fetch(url).
test("alternate shape — intermediate url variable", () => {
  const src = "const url = `/api/w/${id}`;\nconst res = await fetch(url);";
  const { code } = encodePathParams(src);
  assert(code.includes("/api/w/${encodeURIComponent(String(id))}"), `got: ${code}`);
  assert.deepStrictEqual(findRawPathSegments(code), []);
});

// 4a. GUARD catches a genuinely raw path segment (build must fail).
test("guard — flags a genuinely raw path segment", () => {
  const raw = "const res = await fetch(`/api/r/${id}`);";
  const leaks = findRawPathSegments(raw);
  assert(leaks.length === 1 && leaks[0] === "id", `expected [id], got ${JSON.stringify(leaks)}`);
});

// 4b. GUARD is not fooled by the corrupted output the OLD regex produced.
test("guard — rejects corrupted nested-brace rewrite", () => {
  const src = "const res = await fetch(`/api/y/${fn({a: b})}`);";
  const corrupted = oldBrokenRegex(src);
  // Sanity: the old approach really does corrupt this input.
  assert(corrupted !== "const res = await fetch(`/api/y/${encodeURIComponent(String(fn({a: b})))}`);",
    "precondition: old regex should corrupt nested braces");
  let rejected = false;
  try {
    const leaks = findRawPathSegments(corrupted);
    rejected = leaks.length > 0; // not accepted as clean-encoded
  } catch {
    rejected = true; // threw on parse error — also a rejection
  }
  assert(rejected, `guard wrongly accepted corrupted output: ${corrupted}`);
});

// 5. IDEMPOTENCY: second run is a no-op.
test("idempotency — running twice is a no-op", () => {
  const src =
    "const res = await fetch(`/api/resources/${rt}/${rid}/permissions?x=${q}`);";
  const once = encodePathParams(src).code;
  const twice = encodePathParams(once);
  assert.strictEqual(twice.code, once, "second pass changed the code");
  assert.strictEqual(twice.wrapped, 0, "second pass should wrap nothing");
  assert.strictEqual(twice.alreadyWrapped, 2, "both path segments already encoded");
});

// 6. Non-API template literals (e.g. error messages) are never touched.
test("non-API template literal is untouched", () => {
  const src = "throw new Error(`HTTP ${status}: ${statusText}`);";
  const { code, wrapped } = encodePathParams(src);
  assert.strictEqual(code, src);
  assert.strictEqual(wrapped, 0);
});

console.log(failures === 0 ? "\nALL ADVERSARIAL TESTS CONFIRMED" : `\n${failures} TEST(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
