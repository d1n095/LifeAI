#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");

// Temporary exception approved 2026-10-15. It is deliberately bound to every stable
// advisory attribute npm exposes, not merely severity or package name. Removal condition:
// remove as soon as braces publishes a patched compatible release, or Next/Tailwind no
// longer resolve braces <=3.0.3. Review no later than 2027-01-15.
const TEMPORARY_DEV_EXCEPTIONS = Object.freeze([
  Object.freeze({
    package: "braces",
    source: 1240992,
    severity: "high",
    url: "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
    title: "braces vulnerable to stack-exhaustion denial of service through deeply nested patterns",
    range: "<=3.0.3",
  }),
]);

function runAudit(extraArgs = []) {
  const result = spawnSync("npm", ["audit", ...extraArgs, "--json"], {
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
  });
  if (!result.stdout) {
    throw new Error(`npm audit produced no JSON: ${result.stderr || `exit ${result.status}`}`);
  }
  const parsed = JSON.parse(result.stdout);
  if (parsed.error || parsed.message) {
    throw new Error(`npm audit failed: ${parsed.message || parsed.error.summary || "unknown error"}`);
  }
  return parsed;
}

function advisoryMatchesException(packageName, advisory) {
  return TEMPORARY_DEV_EXCEPTIONS.some((exception) =>
    exception.package === packageName &&
    exception.source === advisory.source &&
    exception.severity === advisory.severity &&
    exception.url === advisory.url &&
    exception.title === advisory.title &&
    exception.range === advisory.range
  );
}

function rootAdvisories(report, packageName, seen = new Set()) {
  if (seen.has(packageName)) return [];
  seen.add(packageName);
  const vulnerability = (report.vulnerabilities || {})[packageName];
  if (!vulnerability) return [];
  const roots = [];
  for (const via of vulnerability.via || []) {
    if (typeof via === "string") roots.push(...rootAdvisories(report, via, new Set(seen)));
    else if (via && typeof via === "object") roots.push({ packageName, advisory: via });
  }
  return roots;
}

function compatibleFixAvailable(vulnerability) {
  const fix = vulnerability.fixAvailable;
  return fix === true || Boolean(fix && typeof fix === "object" && fix.isSemVerMajor === false);
}

function evaluateAudits(production, full) {
  const errors = [];
  for (const vulnerability of Object.values(production.vulnerabilities || {})) {
    errors.push(`production vulnerability: ${vulnerability.name} (${vulnerability.severity})`);
  }

  for (const [packageName, vulnerability] of Object.entries(full.vulnerabilities || {})) {
    if (vulnerability.severity === "high" || vulnerability.severity === "critical") {
      const roots = rootAdvisories(full, packageName).filter(
        ({ advisory }) => advisory.severity === "high" || advisory.severity === "critical"
      );
      if (roots.length === 0) {
        errors.push(`unattributed ${vulnerability.severity} dev vulnerability: ${packageName}`);
        continue;
      }
      for (const root of roots) {
        if (!advisoryMatchesException(root.packageName, root.advisory)) {
          errors.push(
            `unapproved ${root.advisory.severity} dev advisory: ${root.packageName} ` +
            `${root.advisory.source} (${root.advisory.url || root.advisory.title || "unknown"})`
          );
        }
      }
    } else {
      const roots = rootAdvisories(full, packageName);
      const actionableRoots = roots.filter(({ packageName: rootPackage }) =>
        compatibleFixAvailable((full.vulnerabilities || {})[rootPackage] || {})
      );
      if ((roots.length === 0 && compatibleFixAvailable(vulnerability)) || actionableRoots.length > 0) {
        errors.push(`actionable dev vulnerability: ${packageName} (${vulnerability.severity})`);
      }
    }
  }
  return [...new Set(errors)].sort();
}

function main() {
  const production = runAudit(["--omit=dev"]);
  const full = runAudit();
  const errors = evaluateAudits(production, full);
  if (errors.length > 0) {
    console.error("npm audit policy failed:");
    for (const error of errors) console.error(`  - ${error}`);
    process.exit(1);
  }
  console.log(
    "npm audit policy: production clean; no actionable/unapproved dev advisories; " +
    "only GHSA-vfj7-8cjw-p6xm is temporarily excepted. OK."
  );
}

module.exports = { advisoryMatchesException, evaluateAudits, rootAdvisories };

if (require.main === module) main();
