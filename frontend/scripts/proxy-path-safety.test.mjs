import assert from "node:assert/strict";
import test from "node:test";

function isUnsafeProxyPathSegment(segment) {
  if (!segment) return true;
  let decoded = segment;
  try {
    decoded = decodeURIComponent(segment);
  } catch {
    return true;
  }
  const normalized = decoded.replace(/\\/g, "/").toLowerCase();
  if (normalized === "." || normalized === "..") return true;
  if (normalized.includes("..") || segment.toLowerCase().includes("%2e")) return true;
  if (normalized.includes("/") || normalized.includes("\0")) return true;
  return false;
}

function assertSafeProxyPath(path) {
  if (!Array.isArray(path) || path.length === 0) {
    throw new Error("empty proxy path");
  }
  if (path.some(isUnsafeProxyPathSegment)) {
    throw new Error("unsafe proxy path");
  }
  return path;
}

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
