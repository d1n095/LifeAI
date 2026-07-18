import { defineConfig, devices } from "@playwright/test";

// Frontend and backend deliberately use DIFFERENT hostnames here (127.0.0.1 vs localhost),
// not just different ports — that's what makes this a genuine cross-origin setup, matching
// production (frontend and backend as separate services) and exercising the parts of the
// cookie/CSRF design that only matter cross-origin. See docs/AUTH_THREAT_MODEL.md.
const FRONTEND_PORT = 3020;
const BACKEND_PORT = 8010;
const ATTACKER_PORT = 9099;

export const FRONTEND_URL = `http://127.0.0.1:${FRONTEND_PORT}`;
export const BACKEND_URL = `http://localhost:${BACKEND_PORT}`;
export const ATTACKER_URL = `http://127.0.0.1:${ATTACKER_PORT}`;

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
    },
    {
      command: `node e2e/attacker-server.js`,
      port: ATTACKER_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 10_000,
    },
  ],
});
