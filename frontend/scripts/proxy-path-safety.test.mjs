import assert from "node:assert/strict";
import test from "node:test";
import { assertSafeProxyPath, isUnsafeProxyPathSegment } from "../lib/proxyPathSafety.ts";

test("encoded and literal traversal segments are rejected", () => {
  for (const segment of ["..", "%2e%2e", "%2E%2E", "%2e%2e%2f", "..%2f", "%2e%2e/secret", "foo%2f.."]) {
    assert.equal(isUnsafeProxyPathSegment(segment), true, segment);
  }
  assert.throws(() => assertSafeProxyPath(["..", "etc", "passwd"]));
  assert.throws(() => assertSafeProxyPath(["%2e%2e", "secret"]));
  assert.throws(() => assertSafeProxyPath([]));
});

test("ordinary API segments stay accepted", () => {
  assert.deepEqual(assertSafeProxyPath(["auth", "login"]), ["auth", "login"]);
  assert.deepEqual(assertSafeProxyPath(["mainai", "founder-ops", "snapshot"]), [
    "mainai",
    "founder-ops",
    "snapshot",
  ]);
});
