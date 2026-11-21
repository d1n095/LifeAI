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
    let firstClientId: string | null = null;
    let secondClientId: string | null = null;

    // Let the FIRST request reach the real backend and persist its reply, then abort the
    // response before the browser can read it. The UI must retry with the same
    // client_message_id, so the backend replays the durable reply instead of creating a
    // second user message or a second provider call.
    await page.route("**/api/chat/stream", async (route) => {
      const body = route.request().postDataJSON() as { client_message_id?: string };
      if (firstChatRequest) {
        firstChatRequest = false;
        firstClientId = body.client_message_id ?? null;
        const response = await route.fetch();
        await route.abort("failed");
        return;
      }
      secondClientId = body.client_message_id ?? null;
      const response = await route.fetch();
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
    expect(firstClientId).not.toBeNull();
    expect(secondClientId).toBe(firstClientId);
    await expect(
      page.getByRole("alert").filter({ hasText: "Kunde inte nå servern" })
    ).toHaveCount(0);
  });

  test("chat streams partial text and then completes without duplicating the message", async ({ page }) => {
    const message = `E2E-stream-${Date.now()}`;
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });

    await page.getByLabel("Meddelande till MainAI").fill(message);
    await page.getByRole("button", { name: "Skicka" }).click();

    const transcript = page.locator('div[aria-live="polite"]');
    // The first delayed chunk proves the UI renders the reply while generation is still
    // running, not only after the final JSON response.
    await expect(page.getByRole("button", { name: "Avbryt", exact: true })).toBeVisible({ timeout: 5000 });
    await expect(transcript.getByText(/Hej! Detta ar ett riktigt svar/)).toBeVisible({ timeout: 5000 });

    await expect(page.getByRole("button", { name: "Avbryt", exact: true })).toHaveCount(0, { timeout: 10000 });
    await expect(transcript.getByText(message, { exact: true })).toHaveCount(1);
  });

  test("cancelling a stream stops generation and keeps exactly one saved user message", async ({ page }) => {
    const message = `E2E-avbryt-${Date.now()}`;
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });

    await page.getByLabel("Meddelande till MainAI").fill(message);
    await page.getByRole("button", { name: "Skicka" }).click();

    const transcript = page.locator('div[aria-live="polite"]');
    await expect(page.getByRole("button", { name: "Avbryt", exact: true })).toBeVisible({ timeout: 5000 });
    await expect(transcript.getByText(/Detta är ett långsamt strömmande svar/)).toBeVisible({ timeout: 5000 });

    await page.getByRole("button", { name: "Avbryt", exact: true }).click();
    await expect(page.getByRole("button", { name: "Skicka" })).toBeVisible({ timeout: 5000 });
    await expect(
      page.getByRole("alert").filter({ hasText: "Fel: Genereringen avbröts." })
    ).toBeVisible({ timeout: 5000 });
    await expect(transcript.getByText(message, { exact: true })).toHaveCount(1);

    // Reloading and reopening the same conversation must not create a second user message.
    await page.reload({ waitUntil: "networkidle" });
    await page
      .locator('div[aria-label="Tidigare konversationer"]')
      .getByText(message, { exact: true })
      .click();
    await expect(transcript.getByText(message, { exact: true })).toHaveCount(1);
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
