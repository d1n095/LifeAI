import { expect, test } from "@playwright/test";
import { BACKEND_URL } from "../playwright.config";

// Run after everything else (see playwright.config.ts — workers: 1, and this file sorts
// last alphabetically) since it deliberately exhausts the per-IP rate limit, which would
// otherwise poison any test that runs immediately afterward in the same window.
test.describe("rate limiting / brute-force protection", () => {
  test("repeated failed logins against one account are eventually cut off (429)", async ({ request }) => {
    const target = { email: "brute-force-target@example.com", password: "wrong-guess" };
    const statuses: number[] = [];
    for (let i = 0; i < 12; i++) {
      const res = await request.post(`${BACKEND_URL}/api/auth/login`, { data: target });
      statuses.push(res.status());
    }
    expect(statuses).toContain(429);
  });

  test("repeated registration attempts from one IP are rate-limited (429)", async ({ request }) => {
    const statuses: number[] = [];
    for (let i = 0; i < 8; i++) {
      const res = await request.post(`${BACKEND_URL}/api/auth/register`, {
        data: { email: `spam-${i}-${Date.now()}@example.com`, password: "SomePassword123!", website: "" },
      });
      statuses.push(res.status());
    }
    expect(statuses).toContain(429);
  });
});
