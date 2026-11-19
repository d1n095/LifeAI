import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Real auth + browser + admin component. Provider endpoints are intercepted to prove
// request identity without activating or invoking a paid provider.
test("admin verifies and activates the exact selected Z.ai model", async ({ page }) => {
  await loginViaUi(page, FRONTEND_URL, "founder@lifeos.local",
    process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!");
  const captured: Record<string, unknown>[] = [];
  await page.route("**/api/admin/providers/status", (route) => route.fulfill({ json: [{
    name: "zai", configured: true, active_chat: false, active_embedding: false,
    chat_verification: null, embedding_verification: null,
  }] }));
  await page.route("**/api/admin/providers/models", (route) => route.fulfill({ json: [{
    provider: "zai", model_id: "glm-5.3", supported_capabilities: ["chat"],
    verification_result: null, metadata_note: "declared",
  }] }));
  await page.route("**/api/admin/providers/verify", async (route) => {
    captured.push(route.request().postDataJSON());
    await route.fulfill({ json: { result: "ok", message: "Verifierad", checked_by: "founder",
      checked_at: new Date().toISOString(), model: "glm-5.3" } });
  });
  await page.route("**/api/admin/providers/config", async (route) => {
    captured.push(route.request().postDataJSON());
    await route.fulfill({ json: {} });
  });
  await page.goto(`${FRONTEND_URL}/admin`);
  await expect(page.getByLabel("Modell för zai chat")).toHaveValue("glm-5.3");
  await expect(page.getByRole("button", { name: "Använd för embedding", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Testa nu", exact: true }).click();
  await expect.poll(() => captured.length).toBe(1);
  await page.getByRole("button", { name: "Använd för chat", exact: true }).click();
  await expect.poll(() => captured.length).toBe(2);
  expect(captured).toEqual([
    { provider: "zai", role: "chat", model: "glm-5.3" },
    { provider: "zai", role: "chat", model: "glm-5.3" },
  ]);
});
