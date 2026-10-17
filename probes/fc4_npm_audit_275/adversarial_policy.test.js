"use strict";
// Examiner adversarial probes for #275 check-npm-audit.js. Usage: POLICY=<path to check-npm-audit.js> node --test adversarial_policy.test.js
// Each test prints MATRIX|id|HOLDS/GAP|detail and never fails, so the full matrix always prints.
const fs = require("node:fs");
const test = require("node:test");
const { evaluateAudits } = require(process.env.POLICY);
const REAL = process.env.REAL_DIR;

const BR = { source: 1240992, name: "braces", dependency: "braces", severity: "high",
  url: "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
  title: "braces vulnerable to stack-exhaustion denial of service through deeply nested patterns", range: "<=3.0.3" };
const MAJOR = { name: "tailwindcss", version: "4.3.3", isSemVerMajor: true };
const rep = (v = {}) => ({ vulnerabilities: v });
const braces = (via, fix = MAJOR, severity = "high") => ({ name: "braces", severity, via, fixAvailable: fix });
const M = (id, holds, detail) => console.log(`MATRIX|${id}|${holds ? "HOLDS" : "GAP"}|${JSON.stringify(detail)}`);
const run = (prod, full) => { try { return evaluateAudits(prod, full); } catch (e) { return [`THREW ${e.message}`]; } };

test("matrix", () => {
  let e;
  e = run(rep(), rep({ braces: braces([BR]) }));
  M("A0 exact dev-only braces advisory (major-only fix) passes", e.length === 0, e);
  e = run(rep(), rep({ braces: braces([{ ...BR, source: 1299999, url: "https://github.com/advisories/GHSA-xxxx-yyyy-zzzz", title: "another braces bug" }]) }));
  M("A1 another advisory on braces cannot inherit the exception", e.length > 0, e);
  e = run(rep({ braces: braces([BR]) }), rep({ braces: braces([BR]) }));
  M("A2 a production occurrence of the exact braces advisory fails", e.length > 0, e);
  e = run(rep(), rep({ braces: braces([BR]), minimist: { name: "minimist", severity: "high", via: [{ source: 1234, severity: "high", url: "https://github.com/advisories/GHSA-aaaa", title: "t", range: "<1" }], fixAvailable: MAJOR } }));
  M("A3 a different high dev advisory fails", e.length > 0, e);
  e = run(rep(), rep({ braces: braces([{ ...BR, severity: "critical" }], MAJOR, "critical") }));
  M("A4 same source re-rated critical fails", e.length > 0, e);
  e = run(rep(), rep({ braces: braces([{ ...BR, range: "<3.0.4" }]) }));
  M("A5 advisory range rewritten after a patch (\"<3.0.4\") fails", e.length > 0, e);
  for (const [id, fix] of [["A6a", true], ["A6b", { name: "braces", version: "3.0.4", isSemVerMajor: false }], ["A6c", { name: "micromatch", version: "4.0.9", isSemVerMajor: false }]]) {
    e = run(rep(), rep({ braces: braces([BR], fix), micromatch: { name: "micromatch", severity: "high", via: ["braces"], fixAvailable: fix } }));
    M(`${id} excepted advisory with a COMPATIBLE fix available (fixAvailable=${JSON.stringify(fix)}) fails`, e.length > 0, e);
  }
  const realNow = Date.now; Date.now = () => Date.parse("2027-02-01T00:00:00Z");
  const RealDate = Date; global.Date = class extends RealDate { constructor(...a) { super(...(a.length ? a : [RealDate.parse("2027-02-01T00:00:00Z")])); } static now() { return RealDate.parse("2027-02-01T00:00:00Z"); } };
  e = run(rep(), rep({ braces: braces([BR]) }));
  global.Date = RealDate; Date.now = realNow;
  M("A7 the exception expires after its review deadline (2027-01-15; clock set to 2027-02-01)", e.length > 0, e);
  e = run(rep(), rep({ ghost: { name: "ghost", severity: "high", via: [], fixAvailable: false } }));
  M("A8 an unattributed high dev node fails", e.length > 0, e);
  e = run(rep(), rep({ a: { name: "a", severity: "high", via: ["b"] }, b: { name: "b", severity: "high", via: ["a"] } }));
  M("A9 a via-cycle terminates and fails closed", e.length > 0 && !String(e).includes("THREW"), e);
  e = run(rep(), rep({ braces: braces([BR]), fg: { name: "fg", severity: "high", via: ["braces", { source: 77, severity: "moderate", url: "u", title: "t", range: "<2" }], fixAvailable: { name: "fg", version: "1.0.1", isSemVerMajor: false } } }));
  M("A10 a compatible-fix moderate advisory on a package that is high only via braces is still blocked", e.length > 0, e);
  e = run(rep(), rep({ wrap: { name: "wrap", severity: "moderate", via: ["root"], fixAvailable: { name: "wrap", version: "2.1.0", isSemVerMajor: false } },
                       root: { name: "root", severity: "moderate", via: [{ source: 42, severity: "moderate", url: "u", title: "t", range: "<3" }], fixAvailable: { name: "parent", version: "4.0.0", isSemVerMajor: true } } }));
  M("A11 (informational) moderate wrapper with its own compatible non-major fix while its root is major-only", e.length > 0, e);
  if (REAL) for (const v of ["10", "11"]) {
    const full = JSON.parse(fs.readFileSync(`${REAL}/full_npm${v}.json`)), prod = JSON.parse(fs.readFileSync(`${REAL}/prod_npm${v}.json`));
    e = run(prod, full);
    M(`R${v} real npm ${v} output at eed3f4a passes the policy`, e.length === 0, e);
  }
});
