import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "./urls";
import { loginViaUi } from "./helpers";

const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

test.describe("Life Workspaces browser journey", () => {
  test("create, persist context, chat, reload, archive and restore", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 10_000 });

    const suffix = Date.now();
    const workspaceName = `E2E Workspace ${suffix}`;
    const goal = `Leverera en granskad rapport ${suffix}`;
    const taskTitle = `Samla in underlag ${suffix}`;
    const decisionTitle = `Publicera varje fredag ${suffix}`;
    const chatMessage = `Vad är målet med ${workspaceName}?`;

    await page.goto(`${FRONTEND_URL}/workspaces`, { waitUntil: "networkidle" });
    await page.getByLabel("Namn").fill(workspaceName);
    await page.getByLabel("Syfte (valfritt)").fill("Ett beständigt projekt med källor, beslut och AI-samtal.");
    await page.getByLabel("Mål (ett per rad)").fill(goal);
    await page.getByRole("button", { name: "Skapa workspace" }).click();

    const card = page.locator("article", { hasText: workspaceName });
    await expect(card).toBeVisible({ timeout: 10_000 });
    await expect(card.getByText("1 mål")).toBeVisible();

    await card.getByRole("link", { name: workspaceName }).click();
    await expect(page.getByRole("heading", { name: workspaceName })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(goal)).toBeVisible();

    await page.getByLabel("Ny uppgift").fill(taskTitle);
    await page.getByRole("button", { name: "Lägg till" }).click();
    await expect(page.getByText(taskTitle, { exact: true })).toBeVisible({ timeout: 10_000 });

    await page.getByLabel("Beslutstitel").fill(decisionTitle);
    await page
      .getByRole("textbox", { name: "Beslut", exact: true })
      .fill("Rapporten publiceras varje fredag efter granskning.");
    await page.getByRole("button", { name: "Registrera beslut" }).click();
    await expect(page.getByText(decisionTitle, { exact: true })).toBeVisible({ timeout: 10_000 });

    await page.getByLabel("Meddelande till MainAI").fill(chatMessage);
    await page.getByRole("button", { name: "Skicka" }).click();
    await expect(page.getByText(chatMessage, { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByText("Hej! Detta ar ett riktigt svar fran den simulerade AI-motorn under E2E-testet.")
    ).toBeVisible({ timeout: 30_000 });

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText(chatMessage, { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(taskTitle, { exact: true })).toBeVisible();
    await expect(page.getByText(decisionTitle, { exact: true })).toBeVisible();
    await expect(page.getByText("Inga superviserade MainAI-uppgifter är kopplade till det här workspaceet ännu.")).toBeVisible();

    // Versioned settings edit: the API rejects a stale expected_version, so a successful
    // save here proves the loaded workspace version round-tripped through the UI.
    await page.getByRole("button", { name: "Redigera" }).click();
    await page.getByLabel("Syfte").fill(`Uppdaterat syfte ${suffix}`);
    await page.getByLabel("Mål (ett per rad)").fill(`Uppdaterat mål ${suffix}`);
    await page.getByRole("button", { name: "Spara workspace" }).click();
    await expect(page.getByText(`Uppdaterat syfte ${suffix}`)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(`Uppdaterat mål ${suffix}`)).toBeVisible();

    await page.getByRole("button", { name: "Synka workspace" }).click();
    await expect(page.getByText(/Senaste synk: Klar/)).toBeVisible({ timeout: 15_000 });

    await page.goto(`${FRONTEND_URL}/workspaces`, { waitUntil: "networkidle" });
    const reloadedCard = page.locator("article", { hasText: workspaceName });
    await reloadedCard.getByRole("button", { name: "Arkivera" }).click();
    await expect(reloadedCard).toHaveCount(0, { timeout: 10_000 });

    await page.getByLabel("Visa arkiverade").check();
    const archivedCard = page.locator("article", { hasText: workspaceName });
    await expect(archivedCard).toBeVisible({ timeout: 10_000 });
    await archivedCard.getByRole("button", { name: "Återställ" }).click();
    await expect(archivedCard.getByRole("button", { name: "Arkivera" })).toBeVisible({ timeout: 10_000 });
  });

  test("a transient create failure is shown and recovers on retry", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 10_000 });

    const suffix = Date.now();
    const workspaceName = `E2E Recovery ${suffix}`;
    let failedOnce = false;
    await page.route("**/api/workspaces", async (route) => {
      if (route.request().method() === "POST" && !failedOnce) {
        failedOnce = true;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Tillfälligt fel. Försök igen." }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto(`${FRONTEND_URL}/workspaces`, { waitUntil: "networkidle" });
    await page.getByLabel("Namn").fill(workspaceName);
    await page.getByRole("button", { name: "Skapa workspace" }).click();
    await expect(
      page.getByRole("alert").filter({ hasText: "Tillfälligt fel" })
    ).toContainText("Tillfälligt fel");
    await expect(page.locator("article", { hasText: workspaceName })).toHaveCount(0);

    await page.getByRole("button", { name: "Skapa workspace" }).click();
    await expect(page.locator("article", { hasText: workspaceName })).toBeVisible({ timeout: 10_000 });
  });
});
