"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "check-npm-audit.js"), "utf8");
function run(report, failed = false) {
  let exit = 0;
  const messages = [];
  try {
    vm.runInNewContext(source, {
      require: () => ({ execSync: () => {
        if (failed) throw Object.assign(new Error("npm exited nonzero"), { stdout: JSON.stringify(report) });
        return JSON.stringify(report);
      } }),
      console: { log: (x) => messages.push(x), error: (x) => messages.push(x) },
      process: { exit: (code) => { exit = code; throw new Error("exit"); } },
    });
  } catch (error) {
    if (error.message !== "exit") throw error;
  }
  return { exit, messages };
}
function report(via, severity = "high") {
  return { vulnerabilities: { demo: { severity, via } },
    metadata: { vulnerabilities: { total: 1, high: severity === "high" ? 1 : 0,
      critical: severity === "critical" ? 1 : 0 } } };
}
test("registry error JSON and incomplete reports fail closed", () => {
  for (const bad of [{ error: { code: "ENOTFOUND" } }, {}, { vulnerabilities: {} }]) {
    const result = run(bad, true);
    assert.equal(result.exit, 1);
    assert.equal(result.messages.some((m) => m.includes("OK.")), false);
  }
});
test("unknown high advisory fails even with nonzero npm exit", () => {
  assert.equal(run(report([{ source: 999999999 }]), true).exit, 1);
});
test("an advisory with no numeric source is still attributed and fails", () => {
  const result = run(report([{ title: "unattributed high finding" }]));
  assert.equal(result.exit, 1);
  assert.equal(result.messages.some((m) => m.includes("unattributed")), true);
});
test("a high/critical count with no attributable advisory fails closed", () => {
  const result = run({ vulnerabilities: { demo: { severity: "high", via: ["demo"] } },
    metadata: { vulnerabilities: { total: 1, high: 1, critical: 0 } } });
  assert.equal(result.exit, 1);
  assert.equal(result.messages.some((m) => m.includes("could not be attributed")), true);
});
test("allowlisted advisories match by GHSA url and legacy source id", () => {
  const byGhsa = run(report([{ url: "https://github.com/advisories/GHSA-mh99-v99m-4gvg" }]));
  assert.equal(byGhsa.exit, 0);
  assert.equal(byGhsa.messages.some((m) => m.includes("OK.")), true);
  const bySource = run(report([{ source: 1130588 }]));
  assert.equal(bySource.exit, 0);
  assert.equal(bySource.messages.some((m) => m.includes("OK.")), true);
});
test("a completed clean scan passes", () => {
  const result = run({ vulnerabilities: {}, metadata: { vulnerabilities: { total: 0 } } });
  assert.equal(result.exit, 0);
  assert.equal(result.messages.some((m) => m.includes("OK.")), true);
});
