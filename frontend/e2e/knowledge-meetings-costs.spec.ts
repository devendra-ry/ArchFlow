import { expect, test } from "@playwright/test";
import { openNewProject, openTool, tab } from "./helpers";

const POLICY = [
  "# Data retention policy", "",
  "## Customer data", "Customer records are kept for 7 years after the account is closed, then deleted.", "",
  "## Incidents", "A P1 incident must be acknowledged within 15 minutes and resolved within 4 hours.", "",
].join("\n");

const VTT = [
  "WEBVTT", "",
  "00:00:10.000 --> 00:00:18.000", "<v Priya Shah>Customers must be able to track their courier on a live map.</v>", "",
  "00:00:19.000 --> 00:00:25.000", "<v Tom Berg>Agreed. We decided to use PostgreSQL with PostGIS for the location data.</v>", "",
  "00:00:26.000 --> 00:00:31.000", "<v Tom Berg>I'll write the ADR by Friday.</v>", "",
  "00:00:32.000 --> 00:00:40.000", "<v Priya Shah>Should we support offline mode for couriers in tunnels?</v>", "",
].join("\n");

test("knowledge base: add a document, ask with citations, search and remove", async ({ page }) => {
  await openNewProject(page, `KB ${Date.now()}`);
  await openTool(page, /Knowledge base/);
  const dialog = page.getByRole("dialog", { name: "Knowledge base" });
  await expect(dialog.getByRole("tab", { name: /Documents \(0\)/ })).toHaveAttribute("aria-selected", "true");
  await dialog.getByLabel("Add documents").setInputFiles({ name: "retention.md", mimeType: "text/markdown", buffer: Buffer.from(POLICY) });
  await expect(dialog.getByText(/Added retention\.md \(2 passages, keywords only\)/)).toBeVisible();
  await expect(dialog.getByRole("cell", { name: "retention.md", exact: true })).toBeVisible();

  await dialog.getByRole("tab", { name: "Ask" }).click();
  await dialog.getByLabel("Question").fill("How fast must a P1 incident be acknowledged?");
  await dialog.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(dialog.locator(".kb-answer")).toContainText("15 minutes");
  await expect(dialog.locator(".kb-citations")).toContainText("retention.md · § Incidents");

  await dialog.getByRole("tab", { name: "Search" }).click();
  await dialog.getByLabel("Question").fill("customer records");
  await dialog.getByRole("button", { name: "Search", exact: true }).click();
  await expect(dialog.locator(".kb-results li").first()).toContainText("§ Customer data");

  await dialog.getByRole("tab", { name: /Documents/ }).click();
  page.once("dialog", (d) => d.accept());
  await dialog.getByRole("button", { name: "Remove retention.md" }).click();
  await expect(dialog.getByText("No documents yet.")).toBeVisible();
});

test("meetings: import a transcript, accept requirements, a decision and an action item", async ({ page }) => {
  const pid = await openNewProject(page, `Meeting ${Date.now()}`);
  await tab(page, "Requirements");
  await page.getByRole("button", { name: "Meeting", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Import a meeting" });
  await dialog.getByLabel("Transcript or recording").setInputFiles({ name: "standup.vtt", mimeType: "text/vtt", buffer: Buffer.from(VTT) });
  await expect(dialog.getByText(/added to the knowledge base/)).toBeVisible();
  await expect(dialog.getByLabel("Title").first()).toHaveValue("Customers must be able to track their courier on a live map");
  await expect(dialog.getByText("🎙 standup.vtt · Priya Shah · 00:00:10")).toBeVisible();
  await expect(dialog.getByLabel("Decision title")).toHaveValue(/PostGIS/);
  await expect(dialog.getByLabel("Owner")).toHaveValue("Tom Berg");
  await dialog.getByRole("button", { name: /^Add \d+ items?$/ }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".req-list")).toContainText("track their courier");
  await expect(page.locator(".req-list")).toContainText("🎙 standup.vtt");

  const decisions = await (await page.request.get(`/api/projects/${pid}/decisions`)).json();
  expect(decisions.some((d: { status: string; decision: string }) => d.status === "proposed" && d.decision.includes("PostGIS"))).toBeTruthy();
  const memory = await (await page.request.get(`/api/projects/${pid}/memory`)).json();
  expect(memory.some((m: { text: string }) => m.text.startsWith("Action: I'll write the ADR by Friday (Tom Berg"))).toBeTruthy();
});

test("cost estimate: line items, assumptions from requirements and a budget", async ({ page }) => {
  const pid = await openNewProject(page, `Costs ${Date.now()}`, "ai-customer-support");
  const r = await page.request.post(`/api/projects/${pid}/ops`, {
    headers: { Origin: "http://localhost:5174" },
    data: { ops: [{ op: "add_requirement", requirement: { title: "Budget", kind: "non_functional", category: "cost",
      description: "Running costs must stay under $2,000 per month." } }] },
  });
  expect(r.ok()).toBeTruthy();
  await openTool(page, /Cost estimate/);
  const dialog = page.getByRole("dialog", { name: "Cost estimate" });
  await expect(dialog.locator(".cost-lines")).toContainText("Claude");
  await expect(dialog.locator(".cost-budget")).toContainText("$2,000");
  await expect(dialog.getByText(/Biggest costs: Claude/)).toBeVisible();
  await dialog.locator(".cost-line").filter({ hasText: "Orders DB" }).click();
  await expect(dialog.getByText(/vCPU instance/)).toBeVisible();
  await dialog.getByText("Assumptions").click();
  await dialog.getByLabel("Monthly active users").fill("1000");
  await dialog.getByRole("button", { name: "Re-estimate" }).click();
  await expect(dialog.getByText("set by you").first()).toBeVisible();
  await expect(dialog.locator(".cost-budget")).toContainText("within budget");
  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "CSV", exact: true }).click();
  expect((await download).suggestedFilename()).toMatch(/costs\.csv$/);
});
