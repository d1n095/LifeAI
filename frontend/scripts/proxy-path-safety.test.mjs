import assert from "node:assert/strict";
import test from "node:test";
import { applyTrustedForwardingHeaders, isClientForgedForwardHeader, runWithTrustedPeer, trustedClientIp } from "../lib/proxyRequestIdentity.ts";
import { assertSafeProxyPath, isUnsafeProxyPathSegment } from "../lib/proxyPathSafety.ts";
import { describeHealth, describeReadiness, describeStop, statusLine } from "../lib/founderHealth.ts";

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

test("R1 request.ip missing uses the captured TCP peer, never client XFF or unknown collapse", () => {
  const forged = new Headers({
    "x-forwarded-for": "198.51.100.1",
    "x-mainai-client-ip": "198.51.100.9",
    "x-mainai-trusted-peer": "198.51.100.8",
  });
  assert.equal(trustedClientIp({ headers: forged }), "unknown");
  const a = runWithTrustedPeer("203.0.113.10", () => trustedClientIp({ headers: forged }));
  const b = runWithTrustedPeer("203.0.113.20", () => trustedClientIp({ headers: forged }));
  assert.equal(a, "203.0.113.10");
  assert.equal(b, "203.0.113.20");
  assert.notEqual(a, b);
  assert.notEqual(a, "unknown");
  const outbound = applyTrustedForwardingHeaders(new Headers(forged), a);
  assert.equal(outbound.get("x-mainai-client-ip"), "203.0.113.10");
  assert.notEqual(outbound.get("x-forwarded-for"), "198.51.100.1");
});

test("ordinary API segments stay accepted", () => {
  assert.deepEqual(assertSafeProxyPath(["auth", "login"]), ["auth", "login"]);
  assert.deepEqual(assertSafeProxyPath(["mainai", "founder-ops", "snapshot"]), [
    "mainai",
    "founder-ops",
    "snapshot",
  ]);
});

test("D4 missing health or STOP evidence is unknown, not reachable or inactive", () => {
  assert.equal(describeHealth(undefined), "Backend-status okänd");
  assert.equal(describeHealth({ status: "unknown", backend_reachable: false, evidence_available: false }), "Backend-status okänd");
  assert.equal(describeHealth({ status: "ok", backend_reachable: true, evidence_available: true }), "Backend nåbar");
  assert.equal(describeHealth({ status: "unavailable", backend_reachable: false, evidence_available: true }), "Backend otillgänglig");
  assert.equal(describeStop(undefined), "STOP-status okänd (fail-closed)");
  assert.equal(describeStop({ blocked: false }), "STOP-status okänd (fail-closed)");
  assert.equal(describeStop({ blocked: false, observability_failed: false }), "STOP inaktiv");
  assert.equal(describeStop({ blocked: true, observability_failed: true }), "STOP-status okänd (fail-closed)");
  const missing = statusLine(
    {
      health: { status: "unknown", backend_reachable: false },
      startup_readiness: { level: "UNKNOWN", evidence_unavailable: true, blocking: ["startup_readiness_unavailable"] },
      stop: { blocked: false },
      safe_launch: { privileged_remote_effects_enabled: false, recall_http_mounted: false },
      first_cycle_go: false,
    },
    null,
    true,
  );
  assert.match(missing, /Backend-status okänd/);
  assert.match(missing, /STOP-status okänd/);
  assert.doesNotMatch(missing, /Backend nåbar/);
  assert.doesNotMatch(missing, /STOP inaktiv/);
});

test("D4 a failed snapshot poll cannot keep a stale reachable backend status", () => {
  const reachable = {
    health: { status: "ok", worker: "alive", backend_reachable: true, evidence_available: true },
    startup_readiness: { level: "READY_FOR_SAFE_INTERNAL_RUN", blocking: [] },
    stop: { blocked: false, observability_failed: false },
    safe_launch: { privileged_remote_effects_enabled: false, recall_http_mounted: false },
    first_cycle_go: false,
  };
  const before = statusLine(reachable, null, true);
  assert.match(before, /Backend nåbar/);
  const afterFailedPoll = statusLine(null, "Simulerat serverfel", true);
  assert.doesNotMatch(afterFailedPoll, /Backend nåbar/);
  assert.doesNotMatch(afterFailedPoll, /STOP inaktiv/);
  assert.match(afterFailedPoll, /otillgänglig/);
});

test("D5 BLOCKED without named blockers cannot be presented as BLOCKED", () => {
  assert.equal(describeReadiness({ level: "BLOCKED", blocking: [] }), "beredskap okänd (fail-closed)");
  assert.equal(describeReadiness({ level: "BLOCKED", blocking: ["provider_disabled"] }), "beredskap BLOCKED (1)");
  assert.equal(describeReadiness({ level: "READY_FOR_SAFE_INTERNAL_RUN", blocking: [] }), "beredskap READY_FOR_SAFE_INTERNAL_RUN");
});
