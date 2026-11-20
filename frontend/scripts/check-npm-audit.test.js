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
test("registry error JSON and incomplete reports fail closed", () => {
  for (const report of [{ error: { code: "ENOTFOUND" } }, {}, { vulnerabilities: {} }]) {
    const result = run(report, true);
    assert.equal(result.exit, 1);
    assert.equal(result.messages.some((m) => m.includes("OK.")), false);
  }
});
test("unknown high advisory fails even with nonzero npm exit", () => {
  assert.equal(run({ vulnerabilities: { demo: { severity: "high", via: [{ source: 999999999 }] } },
    metadata: { vulnerabilities: { total: 1 } } }, true).exit, 1);
});
test("a completed clean scan passes", () => {
  const result = run({ vulnerabilities: {}, metadata: { vulnerabilities: { total: 0 } } });
  assert.equal(result.exit, 0);
  assert.equal(result.messages.some((m) => m.includes("OK.")), true);
});
