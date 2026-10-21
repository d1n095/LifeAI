import assert from "node:assert/strict";
import test from "node:test";
import { applyTrustedForwardingHeaders, isClientForgedForwardHeader, trustedClientIp } from "../lib/proxyRequestIdentity.ts";
import { assertSafeProxyPath, isUnsafeProxyPathSegment } from "../lib/proxyPathSafety.ts";

test("encoded and literal traversal segments are rejected", () => {
  for (const segment of ["..", "%2e%2e", "%2E%2E", "%2e%2e%2f", "..%2f", "%2e%2e/secret", "foo%2f.."]) {
    assert.equal(isUnsafeProxyPathSegment(segment), true, segment);
  }
  assert.throws(() => assertSafeProxyPath(["..", "etc", "passwd"]));
  assert.throws(() => assertSafeProxyPath(["%2e%2e", "secret"]));
  assert.throws(() => assertSafeProxyPath([]));
});

test("PX-9 forged forwarding headers never become the outbound identity", () => {
  const incoming = new Headers({
    "x-forwarded-for": "198.51.100.1",
    "x-real-ip": "198.51.100.1",
    forwarded: "for=198.51.100.1",
    cookie: "session=abc",
  });
  const outbound = new Headers();
  incoming.forEach((value, key) => {
    if (!isClientForgedForwardHeader(key)) outbound.set(key, value);
  });
  applyTrustedForwardingHeaders(outbound, trustedClientIp({ ip: "203.0.113.10", headers: incoming }));
  assert.equal(outbound.get("x-forwarded-for"), "203.0.113.10");
  assert.equal(outbound.get("x-mainai-client-ip"), "203.0.113.10");
  assert.equal(outbound.get("cookie"), "session=abc");
  assert.notEqual(outbound.get("x-forwarded-for"), "198.51.100.1");
});

test("PX-10/PX-11 distinct trusted clients keep distinct proxy identities", () => {
  const a = applyTrustedForwardingHeaders(new Headers(), trustedClientIp({ ip: "203.0.113.10", headers: new Headers() }));
  const b = applyTrustedForwardingHeaders(new Headers(), trustedClientIp({ ip: "203.0.113.20", headers: new Headers() }));
  assert.notEqual(a.get("x-mainai-client-ip"), b.get("x-mainai-client-ip"));
});

test("ordinary API segments stay accepted", () => {
  assert.deepEqual(assertSafeProxyPath(["auth", "login"]), ["auth", "login"]);
  assert.deepEqual(assertSafeProxyPath(["mainai", "founder-ops", "snapshot"]), [
    "mainai",
    "founder-ops",
    "snapshot",
  ]);
});
