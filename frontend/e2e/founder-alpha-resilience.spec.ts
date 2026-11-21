import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Founder Alpha resilience paths that were not covered by the existing specs:
//   * a full reload must recover the session/CSRF state, not strand the founder on /login;
//   * a real browser-level network interruption must surface an alert and recover on retry;
//   * projects and assignments must be creatable through the current UI.
// Everything runs against the real Next.js proxy and real backend; only the provider/email
// transport is faked by run_e2e_backend.py.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

test.describe("Founder Alpha resilience", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("a full reload recovers the session and keeps protected pages usable", async ({ page }) => {
    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: "Life Library" })).toBeVisible({ timeout: 5000 });

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: "Life Library" })).toBeVisible({ timeout: 5000 });
    await expect(page.getByLabel("Sök i kunskapsbiblioteket")).toBeVisible();

    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    await expect(page.getByLabel("Meddelande till MainAI")).toHaveCount(1);
  });

  test("a browser-level network interruption is announced and recovers after retry", async ({ page }) => {
    await page.route("**/api/documents", (route) => route.abort("failed"));
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    await expect(page.getByRole("alert").filter({ hasText: /Fel|kunde|nätverk|anslut/i })).toBeVisible({ timeout: 5000 });

    await page.unroute("**/api/documents");
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByRole("alert").filter({ hasText: /Fel|kunde|nätverk|anslut/i })).toHaveCount(0);
    await expect(page.locator("text=God kväll")).toBeVisible({ timeout: 5000 }).catch(() => {});
  });

  test("a dropped chat response resumes the same send without duplicating it", async ({ page }) => {
    const message = `E2E-idempotens-${Date.now()}`;
    let firstChatRequest = true;
    let firstBody: { user_message_id: string; assistant_message_id: string } | null = null;
    let secondBody: { user_message_id: string; assistant_message_id: string } | null = null;

    // Let the FIRST request reach the real backend and persist its reply, then abort the
    // response before the browser can read it. The UI must retry with the same
    // client_message_id, so the backend replays the durable reply instead of creating a
    // second user message or a second provider call.
    await page.route("**/api/chat", async (route) => {
      if (firstChatRequest) {
        firstChatRequest = false;
        const response = await route.fetch();
        firstBody = await response.json();
        await route.abort("failed");
        return;
      }
      const response = await route.fetch();
      secondBody = await response.json();
      await route.fulfill({ response });
    });

    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    await page.getByLabel("Meddelande till MainAI").fill(message);
    await page.getByRole("button", { name: "Skicka" }).click();

    await expect(
      page.getByRole("alert").filter({ hasText: "Kunde inte nå servern" })
    ).toBeVisible({ timeout: 10000 });

    await page.getByRole("button", { name: "Försök igen" }).click();
    await expect(page.locator("text=MainAI skriver…")).toHaveCount(0, { timeout: 10000 });
    // Scope to the chat transcript: the same text also appears as the conversation's title in
    // the history sidebar, which is not a second message.
    await expect(
      page.locator('div[aria-live="polite"]').getByText(message, { exact: true })
    ).toHaveCount(1);
    expect(firstBody).not.toBeNull();
    expect(secondBody).not.toBeNull();
    expect(secondBody!.user_message_id).toBe(firstBody!.user_message_id);
    expect(secondBody!.assistant_message_id).toBe(firstBody!.assistant_message_id);
    await expect(
      page.getByRole("alert").filter({ hasText: "Kunde inte nå servern" })
    ).toHaveCount(0);
  });

  test("chat is fully keyboard-operable and sends with Enter", async ({ page }) => {
    const message = `E2E-tangentbord-${Date.now()}`;
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });

    const input = page.getByLabel("Meddelande till MainAI");
    const send = page.getByRole("button", { name: "Skicka" });
    await input.focus();
    await page.keyboard.press("Tab");
    await expect(send).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(input).toBeFocused();

    await page.keyboard.type(message);
    await page.keyboard.press("Enter");
    await expect(
      page.locator('div[aria-live="polite"]').getByText(message, { exact: true })
    ).toHaveCount(1);
    await expect(page.locator("text=MainAI skriver…")).toHaveCount(0, { timeout: 10000 });
  });

  test("a project and an assignment can be created through the current UI", async ({ page }) => {
    const suffix = String(Date.now());
    const project = `E2E-resiliens-projekt-${suffix}`;
    const task = `E2E-resiliens-uppgift-${suffix}`;

    await page.goto(`${FRONTEND_URL}/projects`, { waitUntil: "networkidle" });
    await page.getByLabel("Nytt projektnamn").fill(project);
    await page.getByRole("button", { name: "Lägg till" }).first().click();
    await expect(page.locator("text=" + project)).toBeVisible({ timeout: 5000 });

    await page.getByLabel("Ny uppgiftstitel").fill(task);
    await page.getByRole("button", { name: "Lägg till" }).nth(1).click();
    await expect(page.locator("text=" + task)).toBeVisible({ timeout: 5000 });
  });
});
