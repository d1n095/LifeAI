import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Browser-level Founder Alpha acceptance for the Blueprint approval boundary. Authentication
// remains real; only the Blueprint endpoints are intercepted so the source-grounding and
// approval transitions are deterministic and cannot spend provider capacity in CI.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";
const SOURCE_ID = "11111111-1111-1111-1111-111111111111";
const CREATED_AT = "2026-01-02T03:04:05Z";

type Draft = {
  id: string;
  title: string;
  version: number;
  status: "draft" | "approved";
  body: string;
  generated_from_query: string | null;
  source_document_ids: string[];
  source_refs: { document_id: string; similarity: number; excerpt: string }[];
  model_provider: string;
  model_name: string;
  provenance: Record<string, unknown>;
  created_at: string;
  approved_at: string | null;
  approved_by: string | null;
};

function makeDraft(version: number): Draft {
  return {
    id: `00000000-0000-0000-0000-00000000000${version}`,
    title: "Founder Alpha Blueprint",
    version,
    status: "draft",
    body: `# Founder Alpha\n\nKällgrundad version ${version}.`,
    generated_from_query: "tekniska beslut och historik",
    source_document_ids: [SOURCE_ID],
    source_refs: [
      {
        document_id: SOURCE_ID,
        similarity: 0.98,
        excerpt: "Founder Alpha ska bevara original och kräva uttryckligt godkännande.",
      },
    ],
    model_provider: "ollama",
    model_name: "qwen2.5:0.5b",
    provenance: { source_document_ids: [SOURCE_ID] },
    created_at: CREATED_AT,
    approved_at: null,
    approved_by: null,
  };
}

function summary(draft: Draft) {
  return {
    id: draft.id,
    title: draft.title,
    version: draft.version,
    status: draft.status,
    source_count: draft.source_refs.length,
    model_provider: draft.model_provider,
    model_name: draft.model_name,
    created_at: draft.created_at,
    approved_at: draft.approved_at,
  };
}

test.describe("Founder Alpha: source-grounded Blueprint approval", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("creates, regenerates and explicitly approves without client-supplied source or approval authority", async ({
    page,
  }) => {
    const drafts = new Map<string, Draft>();
    let generationPayload: Record<string, unknown> | null = null;
    let approvalRequests = 0;

    await page.route("**/api/blueprint/drafts**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const method = request.method();

      if (path === "/api/blueprint/drafts" && method === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([...drafts.values()].map(summary).sort((a, b) => b.version - a.version)),
        });
        return;
      }
      if (path === "/api/blueprint/drafts" && method === "POST") {
        generationPayload = request.postDataJSON() as Record<string, unknown>;
        const draft = makeDraft(1);
        drafts.set(draft.id, draft);
        await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(draft) });
        return;
      }

      const regenerate = path.match(/^\/api\/blueprint\/drafts\/([^/]+)\/regenerate$/);
      if (regenerate && method === "POST") {
        const previous = drafts.get(regenerate[1]);
        expect(previous).toBeDefined();
        const draft = makeDraft(2);
        drafts.set(draft.id, draft);
        await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(draft) });
        return;
      }

      const approve = path.match(/^\/api\/blueprint\/drafts\/([^/]+)\/approve$/);
      if (approve && method === "POST") {
        approvalRequests += 1;
        const draft = drafts.get(approve[1]);
        expect(draft).toBeDefined();
        const approved: Draft = {
          ...draft!,
          status: "approved",
          approved_at: "2026-01-02T04:05:06Z",
          approved_by: "founder-user-id",
        };
        drafts.set(approved.id, approved);
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(approved) });
        return;
      }

      const detail = path.match(/^\/api\/blueprint\/drafts\/([^/]+)$/);
      if (detail && method === "GET") {
        const draft = drafts.get(detail[1]);
        await route.fulfill({
          status: draft ? 200 : 404,
          contentType: "application/json",
          body: JSON.stringify(draft ?? { detail: "Utkastet hittades inte." }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto(`${FRONTEND_URL}/blueprint`, { waitUntil: "networkidle" });
    await expect(page.getByText("Inga utkast ännu.")).toBeVisible();

    await page.getByLabel("Titel").fill("Founder Alpha Blueprint");
    await page.getByLabel("Fokus / fråga (valfritt)").fill("tekniska beslut och historik");
    await page.getByRole("button", { name: "Generera utkast" }).click();

    await expect(page.getByRole("status")).toHaveText("Utkast skapat (version 1).");
    expect(generationPayload).toEqual({
      title: "Founder Alpha Blueprint",
      query: "tekniska beslut och historik",
    });
    expect(generationPayload).not.toHaveProperty("source_document_ids");
    expect(generationPayload).not.toHaveProperty("status");
    expect(generationPayload).not.toHaveProperty("approved_by");

    const article = page.locator("article");
    await expect(article.getByText("Utkast — väntar på godkännande")).toBeVisible();
    await expect(article.getByText(SOURCE_ID)).toBeVisible();
    await expect(article.getByText(/originalen ändras aldrig/)).toBeVisible();

    await page.getByRole("button", { name: "Regenerera (ny version)" }).click();
    await expect(page.getByRole("status")).toHaveText("Ny version skapad (version 2).");
    await expect(article.getByText("version 2")).toBeVisible();
    expect(approvalRequests).toBe(0);

    await page.getByRole("button", { name: "Godkänn utkast" }).click();
    await expect(page.getByRole("status")).toHaveText("Utkastet är godkänt.");
    await expect(article.locator("span").getByText("Godkänd", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Godkänd" })).toBeDisabled();
    expect(approvalRequests).toBe(1);
  });

  test("keeps an ungrounded generation failure visible and never presents it as approved", async ({ page }) => {
    await page.route("**/api/blueprint/drafts**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === "/api/blueprint/drafts" && request.method() === "GET") {
        await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
        return;
      }
      if (path === "/api/blueprint/drafts" && request.method() === "POST") {
        await route.fulfill({
          status: 422,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Ingen indexerad källa matchade frågan." }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto(`${FRONTEND_URL}/blueprint`, { waitUntil: "networkidle" });
    await page.getByLabel("Titel").fill("Ogrundat utkast");
    await page.getByRole("button", { name: "Generera utkast" }).click();

    await expect(page.getByRole("alert")).toHaveText("Ingen indexerad källa matchade frågan.");
    await expect(page.getByText("Godkänd", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("status")).toHaveCount(0);
  });
});
