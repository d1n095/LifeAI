#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");

// Temporary exception approved 2026-10-15. Bound to every stable advisory attribute npm
// exposes, not merely severity or package name. The review deadline is machine-enforced:
// the exception is invalid at 2027-01-15T00:00:00.000Z and afterwards. Removal conditions:
// remove as soon as braces publishes a patched compatible release, any propagator of this
// advisory has a compatible non-major fix, Next/Tailwind no longer resolve braces <=3.0.3,
// or the review deadline is reached. Review no later than 2027-01-15.
const EXCEPTION_REVIEW_DEADLINE = "2027-01-15";

const TEMPORARY_DEV_EXCEPTIONS = Object.freeze([
  Object.freeze({
    package: "braces",
    source: 1240992,
    severity: "high",
    url: "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
    title: "braces vulnerable to stack-exhaustion denial of service through deeply nested patterns",
    range: "<=3.0.3",
    expires: EXCEPTION_REVIEW_DEADLINE,
  }),
]);

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

function parseDateOnlyUtc(value) {
  if (typeof value !== "string") return null;
  const match = DATE_ONLY.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const ms = Date.UTC(year, month - 1, day);
  const parsed = new Date(ms);
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null;
  }
  return ms;
}

function classifyFix(fix) {
  if (fix === false || fix === null || fix === undefined) return "none";
  if (fix === true) return "compatible";
  if (typeof fix === "object") {
    if (fix.isSemVerMajor === true) return "major-only";
    if (fix.isSemVerMajor === false) return "compatible";
    return "unknown";
  }
  return "unknown";
}

function compatibleFixAvailable(vulnerability) {
  return classifyFix(vulnerability && vulnerability.fixAvailable) === "compatible";
}

function untrustworthyFix(vulnerability) {
  return classifyFix(vulnerability && vulnerability.fixAvailable) === "unknown";
}

function blockingFix(vulnerability) {
  const status = classifyFix(vulnerability && vulnerability.fixAvailable);
  return status === "compatible" || status === "unknown";
}

function isPropagationOnly(vulnerability) {
  const vias = vulnerability && vulnerability.via;
  if (!Array.isArray(vias) || vias.length === 0) return false;
  return vias.every((via) => typeof via === "string");
}

function ownObjectVias(vulnerability) {
  const vias = vulnerability && vulnerability.via;
  if (!Array.isArray(vias)) return [];
  return vias.filter((via) => via && typeof via === "object");
}

function advisoryMatchesException(packageName, advisory, exceptions = TEMPORARY_DEV_EXCEPTIONS) {
  if (!advisory || typeof advisory !== "object") return false;
  return exceptions.some((exception) =>
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
  const vulnerabilities = report && report.vulnerabilities;
  if (!vulnerabilities || typeof vulnerabilities !== "object") return [];
  const vulnerability = vulnerabilities[packageName];
  if (!vulnerability || typeof vulnerability !== "object") return [];
  const vias = Array.isArray(vulnerability.via) ? vulnerability.via : [];
  const roots = [];
  for (const via of vias) {
    if (typeof via === "string") {
      roots.push(...rootAdvisories(report, via, new Set(seen)));
    } else if (via && typeof via === "object") {
      roots.push({ packageName, advisory: via });
    }
  }
  return roots;
}

function exceptionExpiryErrors(exceptions, nowMs) {
  const errors = [];
  if (!Array.isArray(exceptions)) {
    return ["invalid exception list"];
  }
  for (const exception of exceptions) {
    const label = (exception && exception.package) || "unknown";
    const source = exception && exception.source != null ? String(exception.source) : "unknown";
    const expiresMs = parseDateOnlyUtc(exception && exception.expires);
    if (expiresMs === null) {
      errors.push(`malformed exception expiry: ${label} ${source} (${JSON.stringify(exception && exception.expires)})`);
      continue;
    }
    if (nowMs >= expiresMs) {
      errors.push(`exception expired — re-review required: ${label} ${source} (expired ${exception.expires})`);
    }
  }
  return errors;
}

function isHighSeverity(value) {
  return value === "high" || value === "critical";
}

function reportShapeErrors(production, full) {
  const invalid =
    !production || typeof production !== "object" || Array.isArray(production) ||
    !full || typeof full !== "object" || Array.isArray(full) ||
    production.vulnerabilities == null || typeof production.vulnerabilities !== "object" ||
    Array.isArray(production.vulnerabilities) ||
    full.vulnerabilities == null || typeof full.vulnerabilities !== "object" ||
    Array.isArray(full.vulnerabilities);
  return invalid ? ["invalid npm audit report shape"] : [];
}

function exceptedFixError(root, viaPackage) {
  return (
    `excepted advisory now has a compatible fix — remove the exception: ` +
    `${root.packageName} ${root.advisory.source} (via ${viaPackage})`
  );
}

function evaluateAudits(production, full, options = {}) {
  const nowMs = options.now !== undefined ? Number(options.now) : Date.now();
  const exceptions = options.exceptions || TEMPORARY_DEV_EXCEPTIONS;
  if (!Number.isFinite(nowMs)) return ["invalid policy clock"];

  const shapeErrors = reportShapeErrors(production, full);
  if (shapeErrors.length > 0) return shapeErrors;

  const errors = exceptionExpiryErrors(exceptions, nowMs);
  const prodVulns = production.vulnerabilities;
  const fullVulns = full.vulnerabilities;

  for (const vulnerability of Object.values(prodVulns)) {
    if (!vulnerability || typeof vulnerability !== "object") {
      errors.push("invalid production vulnerability entry");
      continue;
    }
    errors.push(`production vulnerability: ${vulnerability.name} (${vulnerability.severity})`);
  }

  for (const [packageName, vulnerability] of Object.entries(fullVulns)) {
    if (!vulnerability || typeof vulnerability !== "object") {
      errors.push(`invalid vulnerability entry: ${packageName}`);
      continue;
    }

    const roots = rootAdvisories(full, packageName);
    const highRoots = roots.filter(({ advisory }) => isHighSeverity(advisory.severity));
    const otherRoots = roots.filter(({ advisory }) => !isHighSeverity(advisory.severity));
    const objectVias = ownObjectVias(vulnerability);
    const independentObjectVias = objectVias.filter(
      (via) => !advisoryMatchesException(packageName, via, exceptions)
    );
    const nodeIsHigh = isHighSeverity(vulnerability.severity);

    if (nodeIsHigh) {
      if (highRoots.length === 0) {
        errors.push(`unattributed ${vulnerability.severity} dev vulnerability: ${packageName}`);
      } else {
        let allHighRootsExcepted = true;
        for (const root of highRoots) {
          if (!advisoryMatchesException(root.packageName, root.advisory, exceptions)) {
            allHighRootsExcepted = false;
            errors.push(
              `unapproved ${root.advisory.severity} dev advisory: ${root.packageName} ` +
              `${root.advisory.source} (${root.advisory.url || root.advisory.title || "unknown"})`
            );
            continue;
          }
          if (blockingFix(fullVulns[root.packageName] || {})) {
            errors.push(exceptedFixError(root, packageName));
          }
        }
        // A6 on a propagator: a compatible/unknown fix on a node that only carries the
        // excepted high advisory (no independent object-shaped advisory of its own).
        const alreadyReportedAsRoot = highRoots.some((root) => root.packageName === packageName);
        if (
          allHighRootsExcepted &&
          independentObjectVias.length === 0 &&
          !alreadyReportedAsRoot &&
          blockingFix(vulnerability)
        ) {
          errors.push(exceptedFixError(highRoots[0], packageName));
        }
      }
    }

    // A10: independently fixable moderate/low object advisories are evaluated on every
    // node, including nodes that are high only because they propagate braces.
    for (const root of otherRoots) {
      if (blockingFix(fullVulns[root.packageName] || {})) {
        errors.push(
          `actionable dev vulnerability: ${packageName} (${root.advisory.severity || vulnerability.severity})`
        );
      }
    }

    // A11: npm 10 reports fixAvailable:true on some propagation-only moderate wrappers
    // while npm 11 reports the parent major-only object. Boolean true on a
    // string-via-only wrapper is therefore NOT independently actionable; root
    // packages decide. An object-shaped non-major wrapper fix, or untrustworthy
    // metadata, is independently actionable and fail-closed.
    const ownFix = classifyFix(vulnerability.fixAvailable);
    const npm10PropagationBoolean =
      isPropagationOnly(vulnerability) && vulnerability.fixAvailable === true;

    if (roots.length === 0 && !nodeIsHigh) {
      if (ownFix === "compatible" || ownFix === "unknown") {
        errors.push(`actionable dev vulnerability: ${packageName} (${vulnerability.severity})`);
      }
    } else if (!npm10PropagationBoolean) {
      if (ownFix === "unknown") {
        errors.push(`untrustworthy fix metadata: ${packageName}`);
      } else if (ownFix === "compatible" && typeof vulnerability.fixAvailable === "object") {
        errors.push(`actionable dev vulnerability: ${packageName} (${vulnerability.severity})`);
      }
    }
  }

  return [...new Set(errors)].sort();
}

function parseAuditStdout(stdout, stderr, status) {
  if (!stdout) {
    throw new Error(`npm audit produced no JSON: ${stderr || `exit ${status}`}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch (error) {
    throw new Error(`invalid npm audit JSON: ${error.message}`);
  }
  if (!parsed || typeof parsed !== "object" || parsed.error || parsed.message) {
    throw new Error(
      `npm audit failed: ${(parsed && (parsed.message || (parsed.error && parsed.error.summary))) || "unknown error"}`
    );
  }
  return parsed;
}

function runAudit(extraArgs = []) {
  const result = spawnSync("npm", ["audit", ...extraArgs, "--json"], {
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
  });
  return parseAuditStdout(result.stdout, result.stderr, result.status);
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
    `only GHSA-vfj7-8cjw-p6xm is temporarily excepted until ${EXCEPTION_REVIEW_DEADLINE}. OK.`
  );
}

module.exports = {
  EXCEPTION_REVIEW_DEADLINE,
  TEMPORARY_DEV_EXCEPTIONS,
  advisoryMatchesException,
  classifyFix,
  compatibleFixAvailable,
  evaluateAudits,
  parseAuditStdout,
  parseDateOnlyUtc,
  rootAdvisories,
};

if (require.main === module) main();
