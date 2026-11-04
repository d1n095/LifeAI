"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  AUDIT_ATTEMPTS,
  EXCEPTION_REVIEW_DEADLINE,
  TEMPORARY_DEV_EXCEPTIONS,
  evaluateAudits,
  isRetryableAuditFailure,
  parseAuditStdout,
  parseDateOnlyUtc,
  runAudit,
} = require("./check-npm-audit.js");

const bracesAdvisory = Object.freeze({
  source: 1240992,
  severity: "high",
  url: "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
  title: "braces vulnerable to stack-exhaustion denial of service through deeply nested patterns",
  range: "<=3.0.3",
});

const MAJOR = Object.freeze({ name: "tailwindcss", version: "4.3.3", isSemVerMajor: true });
const BEFORE_EXPIRY = Date.parse("2027-01-14T23:59:59.999Z");
const ON_EXPIRY = Date.parse("2027-01-15T00:00:00.000Z");
const AFTER_EXPIRY = Date.parse("2027-01-16T00:00:00.000Z");
const CLAUDE_A7_CLOCK = Date.parse("2027-02-01T00:00:00.000Z");

const report = (vulnerabilities = {}) => ({ vulnerabilities });
const bracesNode = (via, fix = MAJOR, severity = "high") => ({
  name: "braces",
  severity,
  via,
  fixAvailable: fix,
});
const run = (prod, full, options) => evaluateAudits(prod, full, options);

test("exception review deadline is the frozen UTC date", () => {
  assert.equal(EXCEPTION_REVIEW_DEADLINE, "2027-01-15");
  assert.equal(TEMPORARY_DEV_EXCEPTIONS[0].expires, "2027-01-15");
  assert.equal(parseDateOnlyUtc(EXCEPTION_REVIEW_DEADLINE), Date.UTC(2027, 0, 15));
});

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

test("A0 exact dev-only braces advisory with a major-only fix passes", () => {
  const errors = run(report(), report({ braces: bracesNode([bracesAdvisory]) }));
  assert.deepEqual(errors, []);
});

test("A1 another advisory on braces cannot inherit the exception", () => {
  const other = {
    ...bracesAdvisory,
    source: 1299999,
    url: "https://github.com/advisories/GHSA-xxxx-yyyy-zzzz",
    title: "another braces bug",
  };
  const errors = run(report(), report({ braces: bracesNode([other]) }));
  assert.ok(errors.some((error) => /unapproved high dev advisory: braces 1299999/.test(error)));
});

test("A2 a production occurrence of the exact braces advisory fails", () => {
  const full = report({ braces: bracesNode([bracesAdvisory]) });
  const errors = run(full, full);
  assert.ok(errors.some((error) => /production vulnerability: braces/.test(error)));
});

test("A3 a different high dev advisory fails", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory]),
    minimist: {
      name: "minimist",
      severity: "high",
      via: [{ source: 1234, severity: "high", url: "https://github.com/advisories/GHSA-aaaa", title: "t", range: "<1" }],
      fixAvailable: MAJOR,
    },
  }));
  assert.ok(errors.some((error) => /unapproved high dev advisory: minimist 1234/.test(error)));
});

test("A4 same source re-rated critical fails", () => {
  const errors = run(
    report(),
    report({ braces: bracesNode([{ ...bracesAdvisory, severity: "critical" }], MAJOR, "critical") })
  );
  assert.ok(errors.some((error) => /unapproved critical dev advisory: braces 1240992/.test(error)));
});

test("A5 advisory range rewritten after a patch fails", () => {
  const errors = run(report(), report({ braces: bracesNode([{ ...bracesAdvisory, range: "<3.0.4" }]) }));
  assert.ok(errors.some((error) => /unapproved high dev advisory: braces 1240992/.test(error)));
});

test("A6a excepted advisory with fixAvailable:true fails", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory], true),
    micromatch: { name: "micromatch", severity: "high", via: ["braces"], fixAvailable: true },
  }));
  assert.ok(errors.some((error) => /excepted advisory now has a compatible fix/.test(error)));
});

test("A6b excepted advisory with a non-major object fix on braces fails", () => {
  const fix = { name: "braces", version: "3.0.4", isSemVerMajor: false };
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory], fix),
    micromatch: { name: "micromatch", severity: "high", via: ["braces"], fixAvailable: fix },
  }));
  assert.ok(errors.some((error) => /excepted advisory now has a compatible fix/.test(error)));
});

test("A6c compatible non-major fix on a package propagating the advisory fails", () => {
  const fix = { name: "micromatch", version: "4.0.9", isSemVerMajor: false };
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory], MAJOR),
    micromatch: { name: "micromatch", severity: "high", via: ["braces"], fixAvailable: fix },
  }));
  assert.ok(errors.some((error) => /excepted advisory now has a compatible fix.*via micromatch/.test(error)));
});

test("A6 malformed fix metadata on the excepted package fails closed", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory], { name: "braces", version: "3.0.4" }),
  }));
  assert.ok(errors.some((error) => /excepted advisory now has a compatible fix/.test(error)));
});

test("A6 incomplete object fix on a propagator fails closed", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory], MAJOR),
    micromatch: { name: "micromatch", severity: "high", via: ["braces"], fixAvailable: {} },
  }));
  assert.ok(errors.some((error) => /excepted advisory now has a compatible fix/.test(error)));
});

test("A6 string-shaped fixAvailable on the excepted package fails closed", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory], "yes"),
  }));
  assert.ok(errors.some((error) => /excepted advisory now has a compatible fix/.test(error)));
});

test("A6 via-cycle involving the excepted advisory terminates and still fail-closes", () => {
  const errors = run(report(), report({
    braces: { name: "braces", severity: "high", via: [bracesAdvisory, "micromatch"], fixAvailable: MAJOR },
    micromatch: { name: "micromatch", severity: "high", via: ["braces"], fixAvailable: true },
  }));
  assert.ok(errors.length > 0);
  assert.ok(errors.every((error) => !error.startsWith("THREW")));
  assert.ok(errors.some((error) => /excepted advisory now has a compatible fix/.test(error)));
});

test("A7 exception is still valid the millisecond before the deadline", () => {
  const errors = run(
    report(),
    report({ braces: bracesNode([bracesAdvisory]) }),
    { now: BEFORE_EXPIRY }
  );
  assert.deepEqual(errors, []);
});

test("A7 exception expires on the review deadline", () => {
  const errors = run(
    report(),
    report({ braces: bracesNode([bracesAdvisory]) }),
    { now: ON_EXPIRY }
  );
  assert.ok(errors.some((error) => /exception expired — re-review required/.test(error)));
});

test("A7 exception expires after the review deadline", () => {
  const errors = run(
    report(),
    report({ braces: bracesNode([bracesAdvisory]) }),
    { now: AFTER_EXPIRY }
  );
  assert.ok(errors.some((error) => /exception expired — re-review required/.test(error)));
});

test("A7 exception expires at Claude's 2027-02-01 probe clock", () => {
  const errors = run(
    report(),
    report({ braces: bracesNode([bracesAdvisory]) }),
    { now: CLAUDE_A7_CLOCK }
  );
  assert.ok(errors.some((error) => /exception expired/.test(error)));
});

test("A7 expired exception fails even when the tree is otherwise clean", () => {
  const errors = run(report(), report(), { now: ON_EXPIRY });
  assert.ok(errors.some((error) => /exception expired/.test(error)));
});

test("A7 missing expiry fails closed", () => {
  const exceptions = [{ ...TEMPORARY_DEV_EXCEPTIONS[0] }];
  delete exceptions[0].expires;
  const errors = run(report(), report(), { now: BEFORE_EXPIRY, exceptions });
  assert.ok(errors.some((error) => /malformed exception expiry/.test(error)));
});

test("A7 empty expiry fails closed", () => {
  const exceptions = [{ ...TEMPORARY_DEV_EXCEPTIONS[0], expires: "" }];
  const errors = run(report(), report(), { now: BEFORE_EXPIRY, exceptions });
  assert.ok(errors.some((error) => /malformed exception expiry/.test(error)));
});

test("A7 non-date expiry fails closed", () => {
  const exceptions = [{ ...TEMPORARY_DEV_EXCEPTIONS[0], expires: "soon" }];
  const errors = run(report(), report(), { now: BEFORE_EXPIRY, exceptions });
  assert.ok(errors.some((error) => /malformed exception expiry/.test(error)));
});

test("A7 datetime expiry (not a date-only value) fails closed", () => {
  const exceptions = [{ ...TEMPORARY_DEV_EXCEPTIONS[0], expires: "2027-01-15T23:59:59.999Z" }];
  const errors = run(report(), report(), { now: BEFORE_EXPIRY, exceptions });
  assert.ok(errors.some((error) => /malformed exception expiry/.test(error)));
});

test("A7 impossible calendar date fails closed", () => {
  const exceptions = [{ ...TEMPORARY_DEV_EXCEPTIONS[0], expires: "2027-02-29" }];
  const errors = run(report(), report(), { now: BEFORE_EXPIRY, exceptions });
  assert.ok(errors.some((error) => /malformed exception expiry/.test(error)));
});

test("A7 expiry tampering with a later date-only value is accepted only as a different exception", () => {
  const exceptions = [{ ...TEMPORARY_DEV_EXCEPTIONS[0], expires: "2099-01-01" }];
  const errors = run(
    report(),
    report({ braces: bracesNode([bracesAdvisory]) }),
    { now: CLAUDE_A7_CLOCK, exceptions }
  );
  assert.deepEqual(errors, []);
  assert.equal(TEMPORARY_DEV_EXCEPTIONS[0].expires, "2027-01-15");
});

test("A7 invalid injected clock fails closed", () => {
  const errors = run(report(), report(), { now: Number.NaN });
  assert.deepEqual(errors, ["invalid policy clock"]);
});

test("A8 an unattributed high dev node fails", () => {
  const errors = run(report(), report({
    ghost: { name: "ghost", severity: "high", via: [], fixAvailable: false },
  }));
  assert.ok(errors.some((error) => /unattributed high dev vulnerability: ghost/.test(error)));
});

test("A9 a via-cycle terminates and fails closed", () => {
  const errors = run(report(), report({
    a: { name: "a", severity: "high", via: ["b"] },
    b: { name: "b", severity: "high", via: ["a"] },
  }));
  assert.ok(errors.some((error) => /unattributed high dev vulnerability: a/.test(error)));
  assert.ok(errors.some((error) => /unattributed high dev vulnerability: b/.test(error)));
});

test("A10 a compatible-fix moderate advisory on a high-via-braces package is still blocked", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory]),
    fg: {
      name: "fg",
      severity: "high",
      via: ["braces", { source: 77, severity: "moderate", url: "u", title: "t", range: "<2" }],
      fixAvailable: { name: "fg", version: "1.0.1", isSemVerMajor: false },
    },
  }));
  assert.ok(errors.some((error) => /actionable dev vulnerability: fg/.test(error)));
  assert.ok(errors.every((error) => !/excepted advisory now has a compatible fix/.test(error)));
});

test("A11 object-shaped non-major wrapper fix is independently actionable", () => {
  const errors = run(report(), report({
    wrap: {
      name: "wrap",
      severity: "moderate",
      via: ["root"],
      fixAvailable: { name: "wrap", version: "2.1.0", isSemVerMajor: false },
    },
    root: {
      name: "root",
      severity: "moderate",
      via: [{ source: 42, severity: "moderate", url: "u", title: "t", range: "<3" }],
      fixAvailable: { name: "parent", version: "4.0.0", isSemVerMajor: true },
    },
  }));
  assert.ok(errors.some((error) => /actionable dev vulnerability: wrap/.test(error)));
});

test("A11 npm 10 boolean-true on a propagation-only moderate wrapper stays non-blocking when the root is major-only", () => {
  const errors = run(report(), report({
    "postcss-nested": {
      name: "postcss-nested",
      severity: "moderate",
      via: ["postcss-selector-parser"],
      fixAvailable: true,
    },
    "postcss-selector-parser": {
      name: "postcss-selector-parser",
      severity: "moderate",
      via: [{ source: 1241232, severity: "moderate", url: "u", title: "t", range: "<7.1.6" }],
      fixAvailable: MAJOR,
    },
  }));
  assert.deepEqual(errors, []);
});

test("A11 npm 11 object major-only on the same wrapper stays non-blocking", () => {
  const errors = run(report(), report({
    "postcss-nested": {
      name: "postcss-nested",
      severity: "moderate",
      via: ["postcss-selector-parser"],
      fixAvailable: MAJOR,
    },
    "postcss-selector-parser": {
      name: "postcss-selector-parser",
      severity: "moderate",
      via: [{ source: 1241232, severity: "moderate", url: "u", title: "t", range: "<7.1.6" }],
      fixAvailable: MAJOR,
    },
  }));
  assert.deepEqual(errors, []);
});

test("sweep: alternate advisory ID cannot reuse the braces exception", () => {
  const errors = run(report(), report({
    braces: bracesNode([{ ...bracesAdvisory, source: 1240993 }]),
  }));
  assert.ok(errors.some((error) => /unapproved high dev advisory: braces 1240993/.test(error)));
});

test("sweep: changed title fails the exception match", () => {
  const errors = run(report(), report({
    braces: bracesNode([{ ...bracesAdvisory, title: "renamed advisory" }]),
  }));
  assert.ok(errors.some((error) => /unapproved high dev advisory/.test(error)));
});

test("sweep: production introduction of a previously dev-only moderate fails", () => {
  const moderate = {
    name: "postcss-selector-parser",
    severity: "moderate",
    via: [{ source: 1241232, severity: "moderate", url: "u", title: "t", range: "<7.1.6" }],
    fixAvailable: MAJOR,
  };
  const errors = run(report({ "postcss-selector-parser": moderate }), report({ "postcss-selector-parser": moderate }));
  assert.ok(errors.some((error) => /production vulnerability: postcss-selector-parser/.test(error)));
});

test("sweep: moving a moderate from dev to production fails even with a major-only fix", () => {
  const vuln = {
    name: "moved",
    severity: "moderate",
    via: [{ source: 1, severity: "moderate", url: "u", title: "t", range: "<1" }],
    fixAvailable: MAJOR,
  };
  const errors = run(report({ moved: vuln }), report({ moved: vuln }));
  assert.ok(errors.some((error) => /production vulnerability: moved \(moderate\)/.test(error)));
});

test("sweep: changed vulnerable range fails", () => {
  const errors = run(report(), report({
    braces: bracesNode([{ ...bracesAdvisory, range: "<=3.0.4" }]),
  }));
  assert.ok(errors.some((error) => /unapproved high dev advisory/.test(error)));
});

test("sweep: registry metadata variation on source package name in the object does not widen the exception", () => {
  const renamed = { ...bracesAdvisory, name: "braces-nightly", dependency: "braces-nightly" };
  const errors = run(report(), report({
    "braces-nightly": { name: "braces-nightly", severity: "high", via: [renamed], fixAvailable: MAJOR },
  }));
  assert.ok(errors.some((error) => /unapproved high dev advisory: braces-nightly 1240992/.test(error)));
});

test("sweep: unknown fix availability on a moderate root fails closed", () => {
  const errors = run(report(), report({
    mystery: {
      name: "mystery",
      severity: "moderate",
      via: [{ source: 9, severity: "moderate", url: "u", title: "t", range: "<1" }],
      fixAvailable: { name: "mystery", version: "1.2.3" },
    },
  }));
  assert.ok(errors.some((error) => /untrustworthy fix metadata: mystery|actionable dev vulnerability: mystery/.test(error)));
});

test("sweep: newly fixable moderate advisory fails after it becomes non-major", () => {
  const errors = run(report(), report({
    parser: {
      name: "parser",
      severity: "moderate",
      via: [{ source: 1241232, severity: "moderate", url: "u", title: "t", range: "<7.1.6" }],
      fixAvailable: { name: "parser", version: "7.1.6", isSemVerMajor: false },
    },
  }));
  assert.ok(errors.some((error) => /actionable dev vulnerability: parser/.test(error)));
});

test("sweep: duplicated vulnerability entries still surface an unapproved sibling", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory]),
    bracesAlias: {
      name: "bracesAlias",
      severity: "high",
      via: [bracesAdvisory],
      fixAvailable: MAJOR,
    },
  }));
  assert.ok(errors.some((error) => /unapproved high dev advisory: bracesAlias 1240992/.test(error)));
});

test("sweep: invalid JSON fails closed", () => {
  assert.throws(() => parseAuditStdout("{", "", 1), /invalid npm audit JSON/);
});

test("sweep: npm error object fails closed", () => {
  assert.throws(
    () => parseAuditStdout(JSON.stringify({ error: { summary: "registry down" } }), "", 1),
    /registry down/
  );
});

test("sweep: unexpected array-shaped audit output fails closed", () => {
  const errors = run([], []);
  assert.deepEqual(errors, ["invalid npm audit report shape"]);
});

test("sweep: missing vulnerabilities map fails closed", () => {
  const errors = run({}, {});
  assert.deepEqual(errors, ["invalid npm audit report shape"]);
});

test("sweep: null report fails closed", () => {
  const errors = run(null, null);
  assert.deepEqual(errors, ["invalid npm audit report shape"]);
});

test("sweep: non-object vulnerability entries fail closed", () => {
  const errors = run(report({ prod: "nope" }), report({ weird: 3 }));
  assert.ok(errors.some((error) => /invalid production vulnerability entry/.test(error)));
  assert.ok(errors.some((error) => /invalid vulnerability entry: weird/.test(error)));
});

test("sweep: exception does not mask a second independently fixable moderate", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory]),
    micromatch: { name: "micromatch", severity: "high", via: ["braces"], fixAvailable: MAJOR },
    leftover: {
      name: "leftover",
      severity: "moderate",
      via: [{ source: 88, severity: "moderate", url: "https://example.invalid/left", title: "t", range: "<2" }],
      fixAvailable: { name: "leftover", version: "2.0.1", isSemVerMajor: false },
    },
  }));
  assert.ok(errors.some((error) => /actionable dev vulnerability: leftover/.test(error)));
});

test("sweep: high-via-braces node with an independently fixable moderate root package is blocked", () => {
  const errors = run(report(), report({
    braces: bracesNode([bracesAdvisory]),
    wrapper: {
      name: "wrapper",
      severity: "high",
      via: ["braces", "parser"],
      fixAvailable: MAJOR,
    },
    parser: {
      name: "parser",
      severity: "moderate",
      via: [{ source: 55, severity: "moderate", url: "u", title: "t", range: "<2" }],
      fixAvailable: { name: "parser", version: "2.0.1", isSemVerMajor: false },
    },
  }));
  assert.ok(errors.some((error) => /actionable dev vulnerability: wrapper/.test(error)));
});

test("sweep: via that is neither string nor object is ignored and high stays unattributed", () => {
  const errors = run(report(), report({
    odd: { name: "odd", severity: "high", via: [12, null, true], fixAvailable: false },
  }));
  assert.ok(errors.some((error) => /unattributed high dev vulnerability: odd/.test(error)));
});

test("parseAuditStdout throws a retryable 503 and does not look like a clean report", () => {
  assert.throws(
    () => parseAuditStdout(
      JSON.stringify({
        error: {
          summary: "503 Service Unavailable - POST https://registry.npmjs.org/-/npm/v1/security/audits/quick - Service Unavailable",
        },
      }),
      "",
      1,
    ),
    (error) => {
      assert.match(error.message, /503 Service Unavailable/);
      assert.equal(isRetryableAuditFailure(error), true);
      assert.doesNotMatch(error.message, /policy: production clean/);
      return true;
    },
  );
  assert.equal(
    isRetryableAuditFailure(new Error("production vulnerability: left-pad (high)")),
    false,
  );
});

test("runAudit retries registry 503 then returns a real report; exhausted 503 still fails", () => {
  const clean = JSON.stringify({ vulnerabilities: {} });
  const unavailable = JSON.stringify({
    error: {
      summary: "503 Service Unavailable - POST https://registry.npmjs.org/-/npm/v1/security/audits/quick - Service Unavailable",
    },
  });
  let calls = 0;
  const recovered = runAudit([], {
    attempts: AUDIT_ATTEMPTS,
    spawn: () => {
      calls += 1;
      if (calls < 3) return { stdout: unavailable, stderr: "", status: 1 };
      return { stdout: clean, stderr: "", status: 0 };
    },
  });
  assert.equal(calls, 3);
  assert.deepEqual(recovered.vulnerabilities, {});

  let failedCalls = 0;
  assert.throws(
    () => runAudit([], {
      attempts: AUDIT_ATTEMPTS,
      spawn: () => {
        failedCalls += 1;
        return { stdout: unavailable, stderr: "", status: 1 };
      },
    }),
    /503 Service Unavailable/,
  );
  assert.equal(failedCalls, AUDIT_ATTEMPTS);
});

test("runAudit does not retry a parsed production vulnerability report", () => {
  let calls = 0;
  const prod = JSON.stringify({
    vulnerabilities: { prod: { name: "prod", severity: "high", via: [] } },
  });
  const parsed = runAudit(["--omit=dev"], {
    spawn: () => {
      calls += 1;
      return { stdout: prod, stderr: "", status: 1 };
    },
  });
  assert.equal(calls, 1);
  assert.match(evaluateAudits(parsed, report())[0], /production vulnerability/);
});
