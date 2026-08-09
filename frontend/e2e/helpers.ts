import fs from "fs";
import path from "path";
import type { APIResponse, BrowserContext, Page } from "@playwright/test";

// Must match backend/scripts/ci/run_e2e_backend.py's default E2E_EMAIL_LOG_PATH.
const EMAIL_LOG_PATH =
  process.env.E2E_EMAIL_LOG_PATH || path.resolve(__dirname, "../../backend/e2e_test_emails.jsonl");

export type CapturedEmail = { to: string; subject: string; body: string };

export function readCapturedEmails(): CapturedEmail[] {
  if (!fs.existsSync(EMAIL_LOG_PATH)) return [];
  return fs
    .readFileSync(EMAIL_LOG_PATH, "utf-8")
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line) as CapturedEmail);
}

export function latestEmailTo(address: string): CapturedEmail | undefined {
  const matches = readCapturedEmails().filter((e) => e.to === address);
  return matches[matches.length - 1];
}

export function extractToken(body: string): string {
  const match = body.match(/token=(\S+)/);
  if (!match) throw new Error(`No token= found in email body: ${body}`);
  return match[1];
}

export function randomEmail(label: string): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `${label}-${rand}@example.com`;
}

/** Captures the csrf_token from login/refresh/me responses on a page, mirroring what
 * frontend/lib/auth.ts does in-memory in the real app — needed here so tests can attach a
 * valid X-CSRF-Token header on raw API calls made outside the app's own UI flow. */
export function trackCsrf(page: Page): { get: () => string | null } {
  let csrf: string | null = null;
  page.on("response", async (res) => {
    if (/\/api\/auth\/(login|refresh|me)$/.test(res.url()) && res.request().method() !== "OPTIONS") {
      try {
        const body = await res.json();
        if (body && typeof body.csrf_token === "string") csrf = body.csrf_token;
      } catch {
        /* non-JSON or empty body */
      }
    }
  });
  return { get: () => csrf };
}

export async function loginViaUi(
  page: Page,
  frontendUrl: string,
  email: string,
  password: string
): Promise<void> {
  await page.goto(`${frontendUrl}/login`, { waitUntil: "networkidle" });
  await page.getByLabel("E-post").fill(email);
  // /login has only one "Lösenord" field (no "Bekräfta lösenord" alongside it), so no
  // exact:true disambiguation is needed here, unlike /register and /reset-password.
  await page.getByLabel("Lösenord").fill(password);
  await page.getByRole("button", { name: "Logga in" }).click();
}

/** MainAI is Founder-only — frontend/app/register/page.tsx no longer renders a form (it
 * redirects to /login), so there is no UI path left to drive. register() itself is still
 * reachable directly in non-production environments (see backend/app/routers/auth.py — the
 * 404 block is scoped to ENVIRONMENT=production, which CI never sets) precisely so tests
 * like this can still exercise account-lifecycle mechanics (duplicate handling, honeypot,
 * password policy, token issuance) at the API layer. What a freshly-registered account can
 * no longer do is log in — see require_founder and login()'s explicit non-founder block —
 * so callers of this helper should expect subsequent login attempts to fail with 401, not
 * succeed the way they used to. */
export async function registerViaApi(
  context: BrowserContext,
  backendUrl: string,
  email: string,
  password: string,
  website: string = ""
): Promise<APIResponse> {
  return context.request.post(`${backendUrl}/api/auth/register`, {
    data: { email, password, website },
  });
}
