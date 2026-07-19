import { defineConfig, devices } from "@playwright/test";
import { ATTACKER_URL, FRONTEND_URL } from "./e2e/urls";

// Runs the E2E suite against a REAL, already-running Dockerfile.combined container (built and
// started by .github/workflows/ci.yml's combined-container-verify job — not by Playwright
// itself, unlike playwright.config.ts). E2E_FRONTEND_URL must point at the container's
// published port; there is no webServer entry for the frontend here because the container is
// already up by the time this config runs. The attacker-origin static server is still a
// separate, ordinary Node process on the CI runner (not inside the container) — Playwright
// manages that one exactly as it does in playwright.config.ts.
export default defineConfig({
  testDir: "./e2e",
  testMatch: ["auth.spec.ts", "security.spec.ts", "account.spec.ts", "rate-limit.spec.ts"],
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never", outputFolder: "playwright-report-combined" }]]
    : "list",
  timeout: 30_000,
  use: {
    baseURL: FRONTEND_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "node e2e/attacker-server.js",
    port: Number(new URL(ATTACKER_URL).port),
    reuseExistingServer: !process.env.CI,
    timeout: 10_000,
  },
});
