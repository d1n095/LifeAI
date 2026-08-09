import { defineConfig, devices } from "@playwright/test";
import { ATTACKER_URL, FRONTEND_URL } from "./e2e/urls";

// FRONTEND_URL/ATTACKER_URL live in ./e2e/urls.ts (env-overridable — see that file's comment
// for why there's no BACKEND_URL here at all: every spec now talks to the backend exclusively
// through the frontend's own same-origin proxy). Re-exported here so existing
// `from "../playwright.config"` imports in the spec files keep working unchanged.
export { ATTACKER_URL, FRONTEND_URL };

const FRONTEND_PORT = 3020;
const ATTACKER_PORT = 9099;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // shared backend/DB state (rate limits, RLS test data) — safer serial
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  timeout: 30_000,
  use: {
    baseURL: FRONTEND_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // The backend is started separately (it's Python — see .github/workflows/ci.yml and
  // docs/OPERATIONS.md's "Kör E2E-testerna lokalt" section), but the frontend and the
  // attacker-origin static server are both plain Node processes Playwright can own.
  webServer: [
    {
      command: `npx next start -p ${FRONTEND_PORT}`,
      url: FRONTEND_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      // Same-origin proxy mode (see frontend/app/api/[...path]/route.ts) — the browser only
      // ever calls this server's own /api/*, never the backend directly. Matches how every
      // real deployment (Docker Compose, the combined Render container) actually works, so
      // this build must NOT set NEXT_PUBLIC_API_URL (see frontend/lib/api.ts's comment on
      // that var). E2E_INTERNAL_API_URL overrides where the proxy forwards to; defaults to
      // where backend/scripts/ci/run_e2e_backend.py listens.
      env: { INTERNAL_API_URL: process.env.E2E_INTERNAL_API_URL || "http://127.0.0.1:8010" },
    },
    {
      command: `node e2e/attacker-server.js`,
      port: ATTACKER_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 10_000,
    },
  ],
});
