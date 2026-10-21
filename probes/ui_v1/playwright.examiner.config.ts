import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./",
  testMatch: "founder_status.spec.ts",
  workers: 1,
  retries: 0,
  reporter: "list",
  timeout: 60_000,
  use: { baseURL: process.env.UI_BASE_URL || "http://127.0.0.1:13073", trace: "off" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
