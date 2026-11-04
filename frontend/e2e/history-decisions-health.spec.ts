import { expect, test, type Page } from "@playwright/test";
import { node, openNewProject, tab } from "./helpers";

async function api(page: Page, method: "post", path: string, data: unknown) {
  const r = await page.request[method](path, { data, headers: { Origin: "http://localhost:5174" } });
  expect(r.ok(), await r.text()).toBeTruthy();
  return r.json();
}

test("history: save a version, compare it, restore it, and undo the restore", async ({ page }) => {
  const pid = await openNewProject(page, `History ${Date.now()}`);
  await api(page, "post", `/api/projects/${pid}/ops`, { ops: [{ op: "add_node", node: { name: "Billing", type: "service" } }] });
  await expect(node(page, "Billing")).toBeVisible();

  await tab(page, "History");
  await page.getByLabel("Version description").fill("Billing only");
  await page.getByRole("button", { name: "Save version" }).click();
  await expect(page.locator(".version-list").getByText("Billing only")).toBeVisible();

  await api(page, "post", `/api/projects/${pid}/ops`, { ops: [{ op: "add_node", node: { name: "Invoices DB", type: "database" } }] });
  await expect(node(page, "Invoices DB")).toBeVisible();

  await page.locator(".version-row").filter({ hasText: "Billing only" }).click();
  await page.getByRole("button", { name: "Compare with now" }).click();
  await expect(page.locator(".diff-text")).toContainText("Invoices DB");
  await page.getByRole("button", { name: "Restore" }).click();
  await page.getByRole("button", { name: /Yes, restore/ }).click();
  await expect(node(page, "Invoices DB")).toHaveCount(0);

  // The state that was replaced is kept as a version, so the restore can be undone.
  const safety = page.locator(".version-row").filter({ hasText: "Before restoring" });
  await expect(safety).toBeVisible();
  await safety.click();
  await page.getByRole("button", { name: "Restore" }).click();
  await page.getByRole("button", { name: /Yes, restore/ }).click();
  await expect(node(page, "Invoices DB")).toBeVisible();
});

test("decisions: compare two drafts, record the decision and adopt it into main", async ({ page }) => {
  const pid = await openNewProject(page, `Decide ${Date.now()}`);
  await api(page, "post", `/api/projects/${pid}/ops`, { ops: [
    { op: "add_requirement", requirement: { title: "Checkout p95 under 300 ms", kind: "non_functional", category: "performance", priority: "critical" } }] });
  for (const [name, extra] of [["draft/simple", []], ["draft/cached", [{ op: "add_node", node: { name: "Cart Cache", type: "cache", technology: "Redis" } },
    { op: "add_edge", edge: { source: "Checkout API", target: "Cart Cache" } }]]] as const) {
    await api(page, "post", `/api/projects/${pid}/branches`, { name });
    await api(page, "post", `/api/projects/${pid}/ops?branch=${encodeURIComponent(name)}`, { ops: [
      { op: "add_node", node: { name: "Checkout API", type: "service" } },
      { op: "add_node", node: { name: "Orders DB", type: "database" } },
      { op: "add_edge", edge: { source: "Checkout API", target: "Orders DB" } }, ...extra] });
  }
  await page.reload();
  await tab(page, "Decisions");
  await page.getByRole("button", { name: /Compare drafts/ }).click();
  const dialog = page.getByRole("dialog", { name: "Compare architectures" });
  await expect(dialog.getByRole("button", { name: /draft\/simple/ })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("button", { name: /draft\/cached/ })).toHaveAttribute("aria-pressed", "true");
  await dialog.getByRole("button", { name: /^Compare 2$/ }).click();
  await expect(dialog.locator(".adr-matrix")).toBeVisible();
  await expect(dialog.getByText(/Offline heuristic|LLM evaluation|Jev decision API/).first()).toBeVisible();
  await expect(dialog.locator(".compare-card")).toHaveCount(2);

  // Priorities re-rank the options live
  await dialog.locator(".prio-row").filter({ hasText: "Performance" }).getByRole("combobox").click();
  await page.getByRole("option", { name: "Critical", exact: true }).click();
  const record = dialog.getByRole("button", { name: /Record decision: adopt/ });
  const chosen = (await record.textContent())!.replace(/.*adopt\s+/, "").trim();
  await record.click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".adr-detail")).toContainText("ADR-001");

  await page.getByRole("button", { name: "Adopt into main" }).click();
  await page.getByRole("button", { name: "Confirm adopt" }).click();
  await expect(page.locator(".adr-detail .status")).toHaveText("accepted");
  await expect(node(page, "Checkout API")).toBeVisible();
  if (chosen === "draft/cached") await expect(node(page, "Cart Cache")).toBeVisible();
});

test("health: score, grouped checks that point at the canvas, and the AI review", async ({ page }) => {
  await openNewProject(page, `Health ${Date.now()}`, "ai-customer-support");
  await tab(page, "Health");
  await expect(page.locator(".score-ring")).toBeVisible();
  await expect(page.locator(".ai-review")).toBeVisible();
  const review = page.locator(".ai-review").getByRole("button", { name: "Review" });
  if (await review.isDisabled()) {
    await expect(page.locator(".ai-review")).toContainText("Add an AI model");  // offline: explained, not hidden
  }
  // A check about a component (completeness checks name a missing capability, so there is nothing to select).
  const first = page.locator(".issue-list .issue[title='Show on the canvas']").first();
  if (await first.count()) {
    await first.click();
    await expect(page.locator(".react-flow__node.selected, .react-flow__node .selected").first().or(page.locator(".diff-banner"))).toBeVisible();
  }
});
