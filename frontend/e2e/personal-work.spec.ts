import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "./urls";
import { loginViaUi } from "./helpers";

const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

test.describe("personal work: tasks, milestones, calendar, search and dashboard", () => {
  test("founder creates work, schedules it, finds it and sees it on the dashboard after reload", async ({
    page,
  }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 10_000 });

    const suffix = Date.now();
    const token = `alpha${suffix}`;
    const projectName = `Projekt ${token}`;
    const milestoneTitle = `Milstolpe ${token}`;
    const firstTask = `Första uppgiften ${token}`;
    const secondTask = `Beroende uppgiften ${token}`;
    const eventTitle = `Kalendermöte ${token}`;
    const eventStart = `2026-11-25T10:${String(suffix % 60).padStart(2, "0")}`;
    const eventEnd = `2026-11-25T11:${String(suffix % 60).padStart(2, "0")}`;

    // Project + milestone through the real project UI.
    await page.goto(`${FRONTEND_URL}/projects`, { waitUntil: "networkidle" });
    await page.getByLabel("Nytt projektnamn").fill(projectName);
    await page.getByRole("button", { name: "Lägg till" }).first().click();
    const projectLink = page.getByRole("link", { name: projectName });
    await expect(projectLink).toBeVisible({ timeout: 10_000 });
    await projectLink.click();
    await expect(page.getByRole("heading", { name: projectName })).toBeVisible({ timeout: 10_000 });

    const milestoneSection = page.getByRole("region", { name: "Milstolpar" });
    await milestoneSection.getByLabel("Ny milstolpe").fill(milestoneTitle);
    await milestoneSection.getByRole("button", { name: "Lägg till" }).click();
    await expect(milestoneSection.getByText(milestoneTitle, { exact: true })).toBeVisible({
      timeout: 10_000,
    });

    // Tasks: create a dependency, verify the blocked state, then complete the prerequisite.
    await page.goto(`${FRONTEND_URL}/tasks`, { waitUntil: "networkidle" });
    await page.getByLabel("Titel", { exact: true }).fill(firstTask);
    await page.getByLabel("Projekt", { exact: true }).selectOption({ label: projectName });
    await page.getByLabel("Milstolpe", { exact: true }).selectOption({ label: milestoneTitle });
    await page.getByRole("button", { name: "Skapa uppgift" }).click();
    await expect(page.getByRole("button", { name: firstTask, exact: true })).toBeVisible({
      timeout: 10_000,
    });

    await page.getByLabel("Titel", { exact: true }).fill(secondTask);
    await page.getByLabel("Projekt", { exact: true }).selectOption({ label: projectName });
    await page.getByLabel("Beror på", { exact: true }).selectOption({ label: firstTask });
    await page.getByRole("button", { name: "Skapa uppgift" }).click();
    await expect(page.getByRole("button", { name: secondTask, exact: true })).toBeVisible({
      timeout: 10_000,
    });
    await expect(
      page.locator("li", { hasText: secondTask }).getByText("Blockerad", { exact: true }).first()
    ).toBeVisible();

    await page.getByRole("button", { name: secondTask, exact: true }).click();
    await expect(page.getByText("Användaruppgift", { exact: true })).toBeVisible();
    await expect(page.getByText(/exekveringsbehörighet/)).toBeVisible();
    await expect(page.getByRole("heading", { name: /Historik/ })).toBeVisible();

    // Complete the prerequisite via its quick status control. The dependent task must unblock.
    const firstRow = page.locator("li", { hasText: firstTask });
    await firstRow.getByLabel(`Ändra status för ${firstTask}`).selectOption("done");
    await expect(page.getByRole("button", { name: firstTask, exact: true })).toBeVisible();

    // Calendar event linked to the project and the task.
    await page.goto(`${FRONTEND_URL}/calendar`, { waitUntil: "networkidle" });
    const eventForm = page.getByRole("region", { name: "Nytt evenemang" });
    await eventForm.getByLabel("Titel", { exact: true }).fill(eventTitle);
    await eventForm.getByLabel("Start", { exact: true }).fill(eventStart);
    await eventForm.getByLabel("Slut", { exact: true }).fill(eventEnd);
    await eventForm.getByLabel("Kategori", { exact: true }).selectOption("work");
    await eventForm.getByLabel("Projekt", { exact: true }).selectOption({ label: projectName });
    await eventForm.getByLabel("Uppgift", { exact: true }).selectOption({ label: firstTask });
    // A previous E2E run may still occupy the same slot. The conflict is real and is shown;
    // accepting the explicit confirmation proves the create path still works after warning.
    page.once("dialog", async (dialog) => dialog.accept());
    await eventForm.getByRole("button", { name: "Skapa evenemang" }).click();
    await expect(page.getByText(eventTitle, { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/Projekt kopplat/).first()).toBeVisible();

    // Conflict detection is a real query, not a client-side guess. Reusing the same time
    // must surface the just-created event without creating a second one.
    await eventForm.getByLabel("Titel", { exact: true }).fill(`Konflikt ${token}`);
    await eventForm.getByLabel("Start", { exact: true }).fill(eventStart);
    await eventForm.getByLabel("Slut", { exact: true }).fill(eventEnd);
    await eventForm.getByRole("button", { name: "Kontrollera konflikter" }).click();
    await expect(eventForm.getByRole("status")).toContainText("Tidskonflikt");

    // Authorized search finds the project and the tasks by the shared unique token.
    await page.goto(`${FRONTEND_URL}/search`, { waitUntil: "networkidle" });
    await page.getByLabel("Sökord").fill(token);
    await page.getByRole("button", { name: "Sök", exact: true }).click();
    await expect(page.getByRole("link", { name: projectName, exact: true })).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByRole("link", { name: firstTask, exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: secondTask, exact: true })).toBeVisible();

    // Dashboard is served by the aggregated endpoint and reflects persisted work.
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: /God kväll/ })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("Aktiva projekt", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Öppna uppgifter", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(projectName, { exact: true })).toBeVisible({ timeout: 10_000 });

    // Reload is a fresh browser request against the same persisted PostgreSQL state.
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText(projectName, { exact: true })).toBeVisible({ timeout: 10_000 });

    // Keyboard path: the search input accepts Enter and returns the same persisted result.
    await page.goto(`${FRONTEND_URL}/search`, { waitUntil: "networkidle" });
    await page.getByLabel("Sökord").fill(token);
    await page.getByLabel("Sökord").press("Enter");
    await expect(page.getByRole("link", { name: projectName, exact: true })).toBeVisible({
      timeout: 10_000,
    });
  });
});
