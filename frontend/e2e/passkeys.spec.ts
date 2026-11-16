import { test, expect, Page } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// End-to-end passkey flow in real Chromium with a CDP WebAuthn VIRTUAL AUTHENTICATOR.
// Unlike soft_webauthn (backend unit tests), the virtual authenticator can assert USER
// VERIFICATION, so this exercises the real biometric-required path (REQUIRE_UV=true) through
// the actual UI (lib/webauthn.ts + PasskeyManager + the login button) and the backend endpoints.
//
// Requires the stack running: backend (/api/auth/webauthn/*) behind the frontend's same-origin
// proxy, and a founder account. Run with E2E_FRONTEND_URL pointing at the running frontend.

const FOUNDER = "founder@lifeos.local";
const PW = "TestFounderPassword123!";

async function addVirtualAuthenticator(page: Page) {
  const client = await page.context().newCDPSession(page);
  await client.send("WebAuthn.enable");
  await client.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal", // platform authenticator (Touch ID / Android fingerprint)
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true, // simulate a successful biometric / PIN
      automaticPresenceSimulation: true,
    },
  });
}

test("passkey enrollment, biometric login, management and session", async ({ page }) => {
  await addVirtualAuthenticator(page);

  // 1) Password login, then enroll a passkey from the account page.
  await loginViaUi(page, FRONTEND_URL, FOUNDER, PW);
  await page.waitForURL(`${FRONTEND_URL}/`, { timeout: 15_000 });

  page.once("dialog", (d) => d.accept("E2E Virtual Key")); // the label prompt()
  await page.goto(`${FRONTEND_URL}/account`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Lägg till passkey" }).click();
  await expect(page.getByText("E2E Virtual Key")).toBeVisible({ timeout: 15_000 });

  // 2) Clear the session and sign in using ONLY the passkey (UV-required ceremony).
  await page.context().clearCookies();
  await page.goto(`${FRONTEND_URL}/login`, { waitUntil: "networkidle" });
  await page.getByLabel("E-post").fill(FOUNDER);
  await page.getByRole("button", { name: "Fortsätt med passkey" }).click();
  await page.waitForURL(`${FRONTEND_URL}/`, { timeout: 15_000 });

  // 3) Session works: the account page loads and still shows the passkey.
  await page.goto(`${FRONTEND_URL}/account`, { waitUntil: "networkidle" });
  await expect(page.getByText("E2E Virtual Key")).toBeVisible();

  // 4) Management + step-up: remove the passkey with the password.
  await page.getByRole("button", { name: "Ta bort" }).click();
  await page.getByLabel("Lösenord för att ta bort passkey").fill(PW);
  await page.getByRole("button", { name: "Ta bort" }).click();
  await expect(page.getByText("Inga passkeys ännu.")).toBeVisible({ timeout: 10_000 });
});

test("passkey step-up rejects a wrong password", async ({ page }) => {
  await addVirtualAuthenticator(page);
  await loginViaUi(page, FRONTEND_URL, FOUNDER, PW);
  await page.waitForURL(`${FRONTEND_URL}/`, { timeout: 15_000 });

  page.once("dialog", (d) => d.accept("Stepup Key"));
  await page.goto(`${FRONTEND_URL}/account`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Lägg till passkey" }).click();
  await expect(page.getByText("Stepup Key")).toBeVisible({ timeout: 15_000 });

  // Wrong password must NOT remove the passkey.
  await page.getByRole("button", { name: "Ta bort" }).click();
  await page.getByLabel("Lösenord för att ta bort passkey").fill("wrong-password");
  await page.getByRole("button", { name: "Ta bort" }).click();
  await expect(page.getByText("Stepup Key")).toBeVisible();

  // Clean up with the correct password (keep the DB tidy for reruns).
  await page.getByLabel("Lösenord för att ta bort passkey").fill(PW);
  await page.getByRole("button", { name: "Ta bort" }).click();
  await expect(page.getByText("Inga passkeys ännu.")).toBeVisible({ timeout: 10_000 });
});
