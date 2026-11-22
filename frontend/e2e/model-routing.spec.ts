import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Real auth + browser + admin component. Provider endpoints are intercepted to prove
// request identity without activating or invoking a paid provider.
test("admin verifies and activates the exact selected Z.ai model", async ({ page }) => {
  await loginViaUi(page, FRONTEND_URL, "founder@lifeos.local",
    process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!");
  // loginViaUi clicks Submit but does not await the authenticated redirect. Navigating
  // immediately can abort login and send this test back to the login page.
  await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
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
  const verifyButtons = page.getByRole("button", { name: "Testa nu", exact: true });
  await expect(verifyButtons.nth(1)).toBeDisabled();
  await expect(page.getByRole("button", { name: "Testar…", exact: true })).toHaveCount(0);
  await verifyButtons.first().click();
  await expect.poll(() => captured.length).toBe(1);
  await page.getByRole("button", { name: "Använd för chat", exact: true }).click();
  await expect.poll(() => captured.length).toBe(2);
  expect(captured).toEqual([
    { provider: "zai", role: "chat", model: "glm-5.3" },
    { provider: "zai", role: "chat", model: "glm-5.3" },
  ]);
});

test("admin discovers installed local models and fills their exact tag", async ({ page }) => {
  await loginViaUi(page, FRONTEND_URL, "founder@lifeos.local",
    process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!");
  await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  await page.route("**/api/admin/providers/status", (route) => route.fulfill({ json: [{
    name: "ollama", configured: true, active_chat: true, active_embedding: false,
    active_chat_model: "llama3.1", active_embedding_model: null,
    chat_verification: null, embedding_verification: null,
  }] }));
  await page.route("**/api/admin/providers/models", (route) => route.fulfill({ json: [{
    provider: "ollama", model_id: "llama3.1", supported_capabilities: ["chat"],
    verification_result: null, metadata_note: "declared",
  }] }));
  await page.route("**/api/admin/providers/local-models", (route) => route.fulfill({ json: {
    runtime: "ollama", reachable: true,
    active_chat_provider: "ollama", active_chat_model: "llama3.1",
    active_embedding_provider: null, active_embedding_model: null,
    selection_requires_verification: true,
    authority_note: "discovery is not verification",
    models: [
      { name: "llama3.1:latest", size_bytes: 4700000000, modified_at: null, digest: null,
        catalog_known: true, declared_capabilities: ["chat"] },
      { name: "qwen2.5:0.5b", size_bytes: 397000000, modified_at: null, digest: null,
        catalog_known: false, declared_capabilities: [] },
    ],
  } }));

  await page.goto(`${FRONTEND_URL}/admin`);
  await expect(page.getByRole("heading", { name: "Lokala modeller" })).toBeVisible();
  await expect(page.getByText("qwen2.5:0.5b", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Använd qwen2.5:0.5b som chat-modell" }).click();
  await expect(page.getByLabel("Egen modell för ollama chat")).toHaveValue("qwen2.5:0.5b");
});
