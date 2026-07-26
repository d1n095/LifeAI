#!/usr/bin/env node
// Fails CI on any high/critical npm audit finding EXCEPT the advisories explicitly
// allowlisted below. This is a dated, ID-scoped exception mechanism, not a general
// suppression switch — every entry must reference a docs/SECURITY_BLOCKERS.md item
// explaining why no fix exists yet and when to remove it.
"use strict";

const { execSync } = require("node:child_process");

const ALLOWLISTED_ADVISORY_SOURCES = new Set([
  // GHSA-mh99-v99m-4gvg (brace-expansion DoS). No eslint-config-next release yet supports
  // eslint@10 (which is the only line using a patched brace-expansion) — see
  // docs/SECURITY_BLOCKERS.md, "brace-expansion / GHSA-mh99-v99m-4gvg". Dev-only lint
  // tooling, never shipped in the production bundle. Remove this line once
  // eslint-config-next (or eslint-plugin-react/import/jsx-a11y) ships a compatible release.
  1124334,
]);

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

const audit = JSON.parse(runAudit());
const offendingSources = new Set();

for (const vuln of Object.values(audit.vulnerabilities || {})) {
  if (vuln.severity !== "high" && vuln.severity !== "critical") continue;
  for (const via of vuln.via || []) {
    if (typeof via === "object" && via !== null && via.source != null) {
      if (!ALLOWLISTED_ADVISORY_SOURCES.has(via.source)) {
        offendingSources.add(`${via.source} (${via.url || via.title || "unknown"})`);
      }
    }
  }
}

if (offendingSources.size > 0) {
  console.error("npm audit: unallowlisted high/critical advisories found:");
  for (const s of offendingSources) console.error(`  - ${s}`);
  process.exit(1);
}

console.log("npm audit: no high/critical findings beyond the documented allowlist. OK.");
