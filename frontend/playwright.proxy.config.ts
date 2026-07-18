import { defineConfig, devices } from "@playwright/test";

// Exercises the same-origin API proxy (frontend/app/api/[...path]/route.ts) specifically —
// unlike playwright.config.ts's deliberately cross-origin setup (see its own comment there),
// this runs the frontend's actual standalone production server (the same server.js Docker
// and Render run — see frontend/Dockerfile) with NEXT_PUBLIC_API_URL unset, so the browser
// only ever calls this server's own /api/*, which forwards to the backend server-side via
// INTERNAL_API_URL. See docs/RENDER_DEPLOY.md for the architecture this validates.
const FRONTEND_PORT = 3021;
const BACKEND_PORT = 8010;

export const PROXY_FRONTEND_URL = `http://127.0.0.1:${FRONTEND_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "same-origin-proxy.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never", outputFolder: "playwright-report-proxy" }]]
    : "list",
  timeout: 30_000,
  use: {
    baseURL: PROXY_FRONTEND_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    // Requires .next/standalone/server.js to already exist, built WITHOUT
    // NEXT_PUBLIC_API_URL, and .next/static + public/ copied into .next/standalone/ — see
    // .github/workflows/ci.yml's "same-origin-proxy-test" job for the exact build steps
    // (mirrors what frontend/Dockerfile does for a real image).
    command: "node .next/standalone/server.js",
    url: PROXY_FRONTEND_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: {
      PORT: String(FRONTEND_PORT),
      INTERNAL_API_URL: `http://127.0.0.1:${BACKEND_PORT}`,
    },
  },
});
