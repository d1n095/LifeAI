import { test, expect } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { FRONTEND_URL } from "./urls";
import { loginViaUi } from "./helpers";

// The Founder Console is the one page the founder is expected to open on a phone. The desktop
// specs (founder-console.spec.ts) prove behaviour, not layout: they run at Desktop Chrome's
// viewport, where the sidebar is always visible and no container is ever forced narrower than
// its content. This spec covers the missing half — that the same page is readable and operable
// at a phone width.
//
// It follows the pattern library-workbench-mobile.spec.ts already established: log in, then
// resize to a 390×844 viewport, rather than adding a second Playwright project (which would
// re-run the whole suite twice and double the gate's browser time).
const FOUNDER_EMAIL = process.env.FOUNDER_EMAIL || "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.FOUNDER_PASSWORD || "TestFounderPassword123!";
const MOBILE_VIEWPORT = { width: 390, height: 844 };

test.describe.configure({ mode: "serial" });
test.use({ baseURL: FRONTEND_URL });

test("the Founder Console is readable and operable at a phone viewport", async ({ page }) => {
  test.setTimeout(120_000);
  await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
  await expect(page.getByRole("button", { name: "Logga ut" })).toBeVisible({ timeout: 30_000 });
  await page.setViewportSize(MOBILE_VIEWPORT);
  await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });

  // The desktop sidebar is not merely squeezed: it is replaced by the collapsible mobile bar,
  // and the nav it hides must still be reachable.
  await expect(page.locator("aside")).toBeHidden();
  const menu = page.getByRole("button", { name: "Öppna meny" });
  await expect(menu).toBeVisible();
  await menu.click();
  await expect(page.getByRole("button", { name: "Stäng meny" })).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#mobile-nav").getByRole("navigation", { name: "Huvudmeny" })).toBeVisible();
  await page.getByRole("button", { name: "Stäng meny" }).click();

  // Same page, same truth: the Living Core still shows a state the page derived from backend
  // measurements, not a placeholder rendered because the layout changed.
  await expect(page.getByRole("heading", { name: "Founder Console" })).toBeVisible({ timeout: 30_000 });
  const core = page.getByTestId("living-core");
  await expect(core).toBeVisible();
  await expect(core).toHaveAttribute("data-core-state", /resting|reasoning|creating|degraded/);
  await expect(page.getByText("MainAI · driftstatus")).toBeVisible();
  await expect(page.getByText("Lokal modell")).toBeVisible();
  await expect(page.getByText("Kräver ditt godkännande")).toBeVisible();
  await expect(page.getByText("Kunde inte nå backend", { exact: false })).toHaveCount(0);

  // No sideways scrolling: at this width every card, table and log must fit or scroll inside
  // its own container. A page-level horizontal scrollbar is the usual symptom of a fixed-width
  // element that only looks fine on a desktop.
  const horizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(horizontalOverflow, "page scrolls sideways at 390px").toBeLessThanOrEqual(1);

  // The composer is the reason to open the console on a phone at all — it must accept input and
  // offer a submit control at this width, not be clipped behind the fold with a hidden button.
  const composer = page.getByPlaceholder("Fråga MainAI…");
  await expect(composer).toBeVisible();
  await composer.fill("Mobiltest: kan du läsa det här?");
  await expect(composer).toHaveValue("Mobiltest: kan du läsa det här?");
  await expect(page.getByRole("button", { name: "Skicka" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Skicka" })).toBeEnabled();

  // Evidence artefact from a real mobile-viewport browser session.
  mkdirSync("../docs/evidence", { recursive: true });
  await page.screenshot({ path: "../docs/evidence/founder-console-mobile.png", fullPage: true });
});
