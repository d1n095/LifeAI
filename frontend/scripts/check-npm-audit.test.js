"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { evaluateAudits } = require("./check-npm-audit.js");

const bracesAdvisory = Object.freeze({
  source: 1240992,
  severity: "high",
  url: "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
  title: "braces vulnerable to stack-exhaustion denial of service through deeply nested patterns",
  range: "<=3.0.3",
});

const report = (vulnerabilities = {}) => ({ vulnerabilities });

test("clean reports pass", () => {
  assert.deepEqual(evaluateAudits(report(), report()), []);
});

test("any production vulnerability fails regardless of severity", () => {
  const errors = evaluateAudits(
    report({ prod: { name: "prod", severity: "low", via: [] } }),
    report()
  );
  assert.match(errors[0], /production vulnerability/);
});

test("the exact dev-only braces advisory passes", () => {
  const full = report({ braces: { name: "braces", severity: "high", via: [bracesAdvisory] } });
  assert.deepEqual(evaluateAudits(report(), full), []);
});

test("propagated packages pass only through the exact braces root", () => {
  const full = report({
    fastGlob: { name: "fastGlob", severity: "high", via: ["braces", "moderatePkg"] },
    braces: { name: "braces", severity: "high", via: [bracesAdvisory] },
    moderatePkg: {
      name: "moderatePkg", severity: "moderate",
      via: [{ source: 42, severity: "moderate", url: "https://example.invalid/moderate" }],
      fixAvailable: { name: "parent", version: "4.0.0", isSemVerMajor: true },
    },
  });
  assert.deepEqual(evaluateAudits(report(), full), []);
});

test("the same source id with a different URL fails", () => {
  const changed = { ...bracesAdvisory, url: "https://example.invalid/different" };
  const errors = evaluateAudits(
    report(), report({ braces: { name: "braces", severity: "high", via: [changed] } })
  );
  assert.match(errors[0], /unapproved high dev advisory/);
});

test("a new high dev advisory fails", () => {
  const advisory = { source: 999, severity: "high", url: "https://example.invalid/new" };
  const errors = evaluateAudits(
    report(), report({ newPkg: { name: "newPkg", severity: "high", via: [advisory] } })
  );
  assert.match(errors[0], /unapproved high dev advisory/);
});

test("a compatible actionable moderate dev fix fails", () => {
  const errors = evaluateAudits(report(), report({
    devPkg: {
      name: "devPkg", severity: "moderate", via: [],
      fixAvailable: { name: "devPkg", version: "1.2.4", isSemVerMajor: false },
    },
  }));
  assert.match(errors[0], /actionable dev vulnerability/);
});

test("a major-only moderate dev migration remains tracked but non-blocking", () => {
  const errors = evaluateAudits(report(), report({
    devPkg: {
      name: "devPkg", severity: "moderate", via: [],
      fixAvailable: { name: "parent", version: "4.0.0", isSemVerMajor: true },
    },
  }));
  assert.deepEqual(errors, []);
});

test("a propagated moderate wrapper cannot override its root's major-only fix", () => {
  const errors = evaluateAudits(report(), report({
    wrapper: { name: "wrapper", severity: "moderate", via: ["root"], fixAvailable: true },
    root: {
      name: "root", severity: "moderate",
      via: [{ source: 42, severity: "moderate", url: "https://example.invalid/root" }],
      fixAvailable: { name: "parent", version: "4.0.0", isSemVerMajor: true },
    },
  }));
  assert.deepEqual(errors, []);
});
