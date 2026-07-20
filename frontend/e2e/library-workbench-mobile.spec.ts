import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// STEG 3/9 of the Founder Knowledge Studio work order explicitly required mobile coverage
// for the new /library and /workbench pages — the desktop flow is already proven by
// founder-knowledge-studio.spec.ts, this spec is specifically about the responsive layout
// (mobile nav collapse, key controls still reachable/usable at a phone viewport), following
// the same page.setViewportSize({width:390,...}) pattern auth.spec.ts already established
// rather than a separate Playwright "project".
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";
const MOBILE_VIEWPORT = { width: 390, height: 844 };

test.describe("Founder Knowledge Studio: mobile layout", () => {
  test("/library is usable at a phone viewport", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    await page.setViewportSize(MOBILE_VIEWPORT);

    // Reach /library via the collapsed mobile nav, not a direct page.goto — proves the nav
    // itself is usable at this viewport, matching how a real phone user would navigate.
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    const desktopSidebarVisible = await page.locator("aside").isVisible().catch(() => false);
    expect(desktopSidebarVisible).toBe(false);
    await page.getByRole("button", { name: "Öppna meny" }).click();
    await page.getByRole("link", { name: "Knowledge Studio" }).click();
    await page.waitForURL(`${FRONTEND_URL}/library`);

    // No horizontal overflow — the page body must not scroll sideways at this width (a
    // common regression when a wide table/import zone isn't given its own scroll container).
    const hasHorizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(hasHorizontalOverflow).toBe(false);

    // Core controls are present and reachable, not just visually squeezed off-screen.
    await expect(page.locator('input[aria-label="Importera till kunskapsbiblioteket"]')).toBeAttached();
    await expect(page.getByLabel("Sök i kunskapsbiblioteket")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sök", exact: true })).toBeVisible();
  });

  test("/workbench is usable at a phone viewport", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    await page.setViewportSize(MOBILE_VIEWPORT);

    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Öppna meny" }).click();
    await page.getByRole("link", { name: "Workbench" }).click();
    await page.waitForURL(`${FRONTEND_URL}/workbench`);

    const hasHorizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(hasHorizontalOverflow).toBe(false);

    const questionBox = page.getByLabel("Fråga till analysen");
    await expect(questionBox).toBeVisible();
    await questionBox.fill("Ett mobiltest av Workbench.");
    await expect(page.getByRole("button", { name: "Starta analys" })).toBeVisible();
  });
});
