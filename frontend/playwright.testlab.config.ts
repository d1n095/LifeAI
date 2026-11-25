import { defineConfig, devices } from "@playwright/test";
import { FRONTEND_URL } from "./e2e/urls";

// Runs against the already-running V6 test lab. Unlike playwright.config.ts there is no
// webServer entry: the real Next.js container is part of docker-compose.testlab.yml, and the
// browser must exercise the same same-origin /api proxy production uses.
export default defineConfig({
  testDir: "./e2e",
  testMatch: ["founder-journey-v6.spec.ts"],
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 90_000,
  reporter: [
    ["list"],
    ["json", { outputFile: process.env.V6_PLAYWRIGHT_JSON || "playwright-founder-v6.json" }],
  ],
  use: {
    baseURL: FRONTEND_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
