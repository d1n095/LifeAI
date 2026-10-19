"use strict";
// Examiner bypass probes for #282 (1e3ee3f) check-npm-audit.js. POLICY=<path> node --test bypass_policy.test.js
// Prints MATRIX|id|HOLDS/GAP|detail; never fails, so the full matrix always prints.
const test = require("node:test");
const { evaluateAudits } = require(process.env.POLICY);
const BR = { source: 1240992, name: "braces", dependency: "braces", severity: "high",
  url: "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
  title: "braces vulnerable to stack-exhaustion denial of service through deeply nested patterns", range: "<=3.0.3" };
const MAJOR = { name: "tailwindcss", version: "4.3.3", isSemVerMajor: true };
const rep = (v = {}, extra = {}) => ({ vulnerabilities: v, ...extra });
const NOW = Date.parse("2026-10-19T00:00:00Z");
const run = (p, f, opts = { now: NOW }) => { try { return evaluateAudits(p, f, opts); } catch (e) { return [`THREW ${e.message}`]; } };
const M = (id, holds, detail) => console.log(`MATRIX|${id}|${holds ? "HOLDS" : "GAP"}|${JSON.stringify(detail)}`);
const HI = { source: 7777, severity: "high", url: "https://github.com/advisories/GHSA-hide", title: "hidden high", range: "<2" };

test("bypass matrix", () => {
  let e;
  e = run(rep(), rep({ x: { name: "x", severity: "moderate", via: [HI], fixAvailable: MAJOR } }));
  M("B1 a HIGH advisory under a node whose own severity says moderate (inconsistent metadata) fails", e.length > 0, e);
  e = run(rep(), rep({ x: { name: "x", severity: "High", via: [{ ...HI, severity: "High" }], fixAvailable: MAJOR } }));
  M("B2 non-canonical severity casing ('High') fails closed", e.length > 0, e);
  e = run(rep(), rep({ x: { name: "x", severity: "unknown-sev", via: [{ ...HI, severity: "unknown-sev" }], fixAvailable: MAJOR } }));
  M("B3 unknown severity vocabulary fails closed", e.length > 0, e);
  e = run(rep(), rep({ braces: { name: "braces", severity: "high", via: [BR], fixAvailable: MAJOR } }), { now: Date.parse("2027-01-14T23:59:59.999Z") });
  M("B4a one ms before the deadline the exact exception still passes", e.length === 0, e);
  e = run(rep(), rep({ braces: { name: "braces", severity: "high", via: [BR], fixAvailable: MAJOR } }), { now: Date.parse("2027-01-15T00:00:00.000Z") });
  M("B4b at the deadline instant the exception is expired", e.some((x) => x.includes("expired")), e);
  e = run(rep(), rep({ braces: { name: "braces", severity: "high", via: [BR], fixAvailable: MAJOR } }), { now: NOW, exceptions: [{ ...BR, package: "braces", expires: "2027-02-30" }] });
  M("B5 malformed expiry date (2027-02-30) fails closed", e.some((x) => x.includes("malformed")), e);
  e = run({}, rep());
  M("B6 production report missing `vulnerabilities` fails closed", e.length > 0, e);
  e = run(rep({}, { metadata: { vulnerabilities: { high: 2, total: 2 } } }), rep());
  M("B7 production report with empty vulnerabilities but metadata counting 2 highs fails closed", e.length > 0, e);
  e = run(rep(), rep({}, { metadata: { vulnerabilities: { critical: 1, total: 1 } } }));
  M("B8 full report with empty vulnerabilities but metadata counting a critical fails closed", e.length > 0, e);
  e = run(rep(), rep({ x: { name: "x", severity: "high", via: "braces", fixAvailable: MAJOR }, braces: { name: "braces", severity: "high", via: [BR], fixAvailable: MAJOR } }));
  M("B9 high node with non-array `via` fails closed", e.length > 0, e);
  e = run(rep(), rep({ braces: { name: "braces", severity: "high", via: [BR], fixAvailable: { name: "braces", version: "3.0.4" } } }));
  M("B10 fixAvailable object without isSemVerMajor (unknown) on the excepted root fails closed", e.length > 0, e);
  e = run(rep(), rep({ braces: { name: "braces", severity: "high", via: [BR, { ...HI, severity: "moderate", source: 8888 }], fixAvailable: MAJOR } }));
  M("B11 (info) excepted root also carrying a non-fixable moderate advisory passes (moderate major-only is policy)", e.length === 0, e);
  e = run(rep(), rep({ braces: { name: "braces", severity: "high", via: [BR], fixAvailable: MAJOR } }), { now: "not-a-time" });
  M("B12 invalid clock fails closed", e.length > 0, e);
  e = run(rep(), rep({ x: { name: "x", severity: "high", via: [{ ...BR }], fixAvailable: MAJOR } }));
  M("B13 the exact braces advisory attributed to a DIFFERENT package does not inherit the exception", e.length > 0, e);
});
