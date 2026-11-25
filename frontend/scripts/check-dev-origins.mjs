// CI guard for the dev-mode hydration fix: NEXT_ALLOWED_DEV_ORIGINS must reach
// next.config.mjs's `allowedDevOrigins`.
//
// Symptom this prevents regressing: with `next dev --hostname 0.0.0.0` served through a
// preview host, the cross-origin /_next/webpack-hmr request is blocked, React's hydrateRoot()
// never resolves its initial server response, and every page renders but stays unhydrated
// (AuthGuard's client never initialises, no handlers fire). It looks like an application bug
// and is invisible to `next build`, so it needs an explicit check.
//
// Runs with no dependencies and no Next.js install: next.config.mjs is plain JS.
import assert from "node:assert/strict";

import nextConfig from "../next.config.mjs";

const expected = (process.env.NEXT_ALLOWED_DEV_ORIGINS || "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const actual = nextConfig.allowedDevOrigins;

assert.ok(Array.isArray(actual), "next.config.mjs must define allowedDevOrigins as an array.");
assert.deepEqual(
  actual,
  expected,
  `allowedDevOrigins ${JSON.stringify(actual)} does not match NEXT_ALLOWED_DEV_ORIGINS ${JSON.stringify(expected)}.`,
);

console.log(
  `allowedDevOrigins wiring OK (${actual.length} origin(s): ${actual.join(", ") || "none"})`,
);
