#!/usr/bin/env node
// Fails CI on any high/critical npm audit finding EXCEPT the advisories explicitly
// allowlisted below. This is a dated, GHSA-scoped exception mechanism, not a general
// suppression switch — every entry must reference a docs/SECURITY_BLOCKERS.md item
// explaining why no fix exists yet, and every entry expires.
//
// Hardening rules enforced here:
//   * an entry must be a real GHSA id, carry a reason and a future expiry date;
//   * matching is by GHSA (stable) or legacy numeric source id (advisory-database churn);
//   * an expired entry fails the job instead of silently widening the exception;
//   * a high/critical advisory that cannot be attributed at all fails closed — the
//     previous version skipped any `via` object without a numeric source.
"use strict";

const { execSync } = require("node:child_process");

const ALLOWLIST = [
  {
    // GHSA-mh99-v99m-4gvg (brace-expansion DoS). No eslint-config-next release yet supports
    // eslint@10 (which is the only line using a patched brace-expansion) — see
    // docs/SECURITY_BLOCKERS.md, "brace-expansion / GHSA-mh99-v99m-4gvg". Dev-only lint
    // tooling, never shipped in the production bundle.
    ghsa: "GHSA-mh99-v99m-4gvg",
    sourceIds: [1124334, 1130588, 1130591],
    expires: "2027-02-28",
    reason:
      "brace-expansion reached only through eslint dev tooling; no compatible patched release",
  },
  {
    // sharp/libheif decoder advisories. The project pins overrides.sharp and never imports
    // next/image (verified in frontend/app and frontend/components).
    ghsa: "GHSA-rgj7-g3m4-5g8c",
    sourceIds: [1193725],
    expires: "2027-02-28",
    reason:
      "sharp decoder not reachable: overrides pin plus no next/image import in the app",
  },
  {
    ghsa: "GHSA-wq5f-xc86-pv6w",
    sourceIds: [1241331],
    expires: "2027-02-28",
    reason:
      "sharp decoder not reachable: overrides pin plus no next/image import in the app",
  },
  {
    // picomatch reached only through tailwind/chokidar and the eslint resolver chain.
    ghsa: "GHSA-fc5q-39xc-vjpf",
    sourceIds: [1247710],
    expires: "2027-02-28",
    reason:
      "picomatch reached only through build/lint tooling, never the production bundle",
  },
];

const GHSA_PATTERN = /^GHSA(-[0-9a-z]{4}){3}$/i;

function fail(message) {
  console.error(message);
  process.exit(1);
}

function buildAllowlist() {
  const byGhsa = new Map();
  const bySource = new Map();
  for (const entry of ALLOWLIST) {
    if (!entry || typeof entry.ghsa !== "string" || !GHSA_PATTERN.test(entry.ghsa)) {
      fail(`npm audit allowlist: invalid GHSA id: ${JSON.stringify(entry && entry.ghsa)}`);
    }
    if (typeof entry.reason !== "string" || entry.reason.trim().length < 20) {
      fail(`npm audit allowlist: ${entry.ghsa} needs a written reason.`);
    }
    const expiresAt = Date.parse(entry.expires);
    if (!Number.isFinite(expiresAt)) {
      fail(`npm audit allowlist: ${entry.ghsa} has an invalid expiry date.`);
    }
    if (expiresAt < Date.now()) {
      fail(
        `npm audit allowlist: ${entry.ghsa} expired on ${entry.expires}; ` +
          "re-triage the finding or remove the exception."
      );
    }
    byGhsa.set(entry.ghsa.toUpperCase(), entry);
    for (const source of entry.sourceIds || []) {
      if (!Number.isInteger(source)) {
        fail(`npm audit allowlist: ${entry.ghsa} has a non-integer source id.`);
      }
      bySource.set(source, entry);
    }
  }
  return { byGhsa, bySource };
}

function runAudit() {
  try {
    return execSync("npm audit --json", { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 });
  } catch (err) {
    // npm audit exits non-zero the moment any vulnerability is found — the JSON report is
    // still written to stdout, so a non-zero exit here is not itself a failure to handle.
    if (err.stdout) return err.stdout;
    throw err;
  }
}

function advisoryIdentity(via) {
  const url = typeof via.url === "string" ? via.url : "";
  const match = url.match(/GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}/i);
  const ghsa = match
    ? match[0].toUpperCase()
    : typeof via.ghsa === "string" && GHSA_PATTERN.test(via.ghsa)
      ? via.ghsa.toUpperCase()
      : null;
  const source = Number.isInteger(via.source) ? via.source : null;
  return { ghsa, source };
}

const { byGhsa, bySource } = buildAllowlist();
const audit = JSON.parse(runAudit());
// A registry/network/auth error is not a clean audit. npm can return JSON even
// when no vulnerability scan completed; never turn that into an empty green result.
if (audit.error || !audit.vulnerabilities || typeof audit.vulnerabilities !== "object" ||
    !audit.metadata || !audit.metadata.vulnerabilities ||
    !Number.isInteger(audit.metadata.vulnerabilities.total)) {
  fail("npm audit: no valid completed audit report; refusing a green result.");
}

const offending = new Set();
const usedAllowlist = new Set();
let attributed = 0;

for (const vuln of Object.values(audit.vulnerabilities || {})) {
  if (vuln.severity !== "high" && vuln.severity !== "critical") continue;
  for (const via of vuln.via || []) {
    // A string `via` names the vulnerable package chain; the actual advisory appears as
    // an object on its own entry and is attributed there.
    if (typeof via === "string" || via === null || typeof via !== "object") continue;
    attributed += 1;
    const { ghsa, source } = advisoryIdentity(via);
    const entry = (ghsa && byGhsa.get(ghsa)) || (source !== null && bySource.get(source));
    if (entry) {
      usedAllowlist.add(entry.ghsa);
      continue;
    }
    offending.add(
      `${ghsa || (source !== null ? source : "unattributed")} ` +
        `(${via.url || via.title || "unknown"})`
    );
  }
}

const totals = audit.metadata.vulnerabilities;
const highCriticalPackages = (totals.high || 0) + (totals.critical || 0);
if (highCriticalPackages > 0 && attributed === 0) {
  // The report claims high/critical findings but exposes no advisory we can attribute.
  // Treat that as an unverifiable scan, never as a pass.
  fail("npm audit: high/critical findings could not be attributed to any advisory.");
}

if (offending.size > 0) {
  console.error("npm audit: unallowlisted high/critical advisories found:");
  for (const item of offending) console.error(`  - ${item}`);
  process.exit(1);
}

for (const entry of ALLOWLIST) {
  if (!usedAllowlist.has(entry.ghsa)) {
    console.log(
      `npm audit: allowlist entry ${entry.ghsa} is no longer reported; remove it when convenient.`
    );
  }
}

console.log("npm audit: no high/critical findings beyond the documented allowlist. OK.");
