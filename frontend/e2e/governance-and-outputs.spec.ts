import { expect, test, type Page } from "@playwright/test";
import { node, openNewProject, openTool, tab } from "./helpers";

async function api(page: Page, path: string, data: unknown) {
  const r = await page.request.post(path, { data, headers: { Origin: "http://localhost:5174" } });
  expect(r.ok(), await r.text()).toBeTruthy();
  return r.json();
}

const reqRow = (page: Page, title: string) => page.locator(".req-row").filter({ hasText: title });

/** Open a requirement's details (the row toggles, and stays open across filters). */
async function openReq(page: Page, title: string) {
  const row = reqRow(page, title);
  if ((await row.getAttribute("aria-expanded")) !== "true") await row.click();
  await expect(row).toHaveAttribute("aria-expanded", "true");
}

test("requirements: soft delete, restore from Deleted, and the change history", async ({ page }) => {
  const pid = await openNewProject(page, `Req history ${Date.now()}`);
  await api(page, `/api/projects/${pid}/ops`, { ops: [
    { op: "add_requirement", requirement: { title: "Customers can track an order", priority: "high" } }] });
  await tab(page, "Requirements");
  await openReq(page, "Customers can track an order");
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(reqRow(page, "Customers can track an order")).toHaveCount(0);

  // History is recorded when the live document is stored (debounced), so let the delete land first.
  const actions = async () => (await (await page.request.get(`/api/projects/${pid}/requirements/REQ-001/history`)).json())
    .map((h: { action: string }) => h.action);
  await expect.poll(actions, { timeout: 15_000 }).toContain("deleted");
  await page.getByRole("tab", { name: /^Deleted \d/ }).click();
  await openReq(page, "Customers can track an order");
  await page.getByRole("button", { name: "Restore", exact: true }).click();
  await expect.poll(actions, { timeout: 15_000 }).toContain("restored");
  await page.getByRole("tab", { name: /^All \d/ }).click();
  await expect(reqRow(page, "Customers can track an order")).toBeVisible();

  await openReq(page, "Customers can track an order");
  await page.locator(".req-edit").getByRole("tab", { name: "History", exact: true }).click();
  const history = page.locator(".req-history");
  await expect(history.getByText("Created")).toBeVisible();
  await expect(history.getByText("Deleted")).toBeVisible();
  await expect(history.getByText("Restored")).toBeVisible();
});

test("requirements: import from a Markdown document, review and accept", async ({ page }) => {
  await openNewProject(page, `Import ${Date.now()}`);
  await tab(page, "Requirements");
  await page.getByRole("button", { name: "⇪ Import" }).click();
  const dialog = page.getByRole("dialog", { name: "Import requirements from a document" });
  await dialog.getByLabel("Document").setInputFiles({
    name: "prd.md", mimeType: "text/markdown",
    buffer: Buffer.from([
      "# Parcel tracking PRD", "", "## Functional requirements", "",
      "- The system shall let customers track a parcel by its tracking number.",
      "- The system shall notify customers by SMS when a parcel is out for delivery.", "",
      "## Quality", "", "- Tracking pages must load in under 500 ms at the 95th percentile.", "",
    ].join("\n")),
  });
  await expect(dialog.getByText(/read with ArchFlow's Markdown reader · requirements found by the offline rules/)).toBeVisible();
  const add = dialog.getByRole("button", { name: /^Add \d+ requirements?$/ });
  await expect(add).toBeEnabled();
  await add.click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".req-row").filter({ hasText: /track a parcel/i })).toBeVisible();
  await expect(page.locator(".req-list")).toContainText(/from prd\.md/);
});

test("requirements: a document import still finishes when the server answers with plain JSON", async ({ page }) => {
  await openNewProject(page, `Import json ${Date.now()}`);
  await tab(page, "Requirements");
  // Without the event-stream Accept header the server answers the old way: the whole proposal as JSON.
  await page.route("**/requirements/import?*", (route) =>
    route.continue({ headers: { ...route.request().headers(), accept: "application/json" } }));
  await page.getByRole("button", { name: "⇪ Import" }).click();
  const dialog = page.getByRole("dialog", { name: "Import requirements from a document" });
  await dialog.getByLabel("Document").setInputFiles({
    name: "plain.md", mimeType: "text/markdown", buffer: Buffer.from("# Spec\n\n- The system shall export invoices.\n"),
  });
  await expect(dialog.getByRole("button", { name: /^Add \d+ requirements?$/ })).toBeEnabled();
  await expect(dialog.getByText(/connection closed/)).toHaveCount(0);
});

test("requirements: a document import shows its steps and can be cancelled", async ({ page }) => {
  await openNewProject(page, `Import cancel ${Date.now()}`);
  await tab(page, "Requirements");
  // Hold the upload so the progress panel stays up long enough to cancel.
  let release = () => {};
  await page.route("**/requirements/import?*", async (route) => {
    await new Promise<void>((r) => { release = r; });
    await route.continue().catch(() => {});
  });
  await page.getByRole("button", { name: "⇪ Import" }).click();
  const dialog = page.getByRole("dialog", { name: "Import requirements from a document" });
  await dialog.getByLabel("Document").setInputFiles({
    name: "slow.md", mimeType: "text/markdown", buffer: Buffer.from("# Spec\n\n- The system shall export invoices.\n"),
  });
  const progress = dialog.getByRole("status");
  await expect(progress).toContainText("Reading slow.md");
  await expect(progress).toContainText("Find requirements");
  await progress.getByRole("button", { name: "Cancel" }).click();
  release();
  await expect(dialog.getByText(/Import cancelled/)).toBeVisible();
  await expect(progress).toHaveCount(0);
  await expect(dialog.getByLabel("Document")).toBeEnabled();
});

test("approvals: request a review on a saved version and approve it", async ({ page }) => {
  const pid = await openNewProject(page, `Approve ${Date.now()}`);
  await api(page, `/api/projects/${pid}/ops`, { ops: [{ op: "add_node", node: { name: "Orders API", type: "service" } }] });
  await expect(node(page, "Orders API")).toBeVisible();
  await tab(page, "History");
  await page.getByLabel("Version description").fill("Orders API v1");
  await page.getByRole("button", { name: "Save version" }).click();
  const row = page.locator(".version-row").filter({ hasText: "Orders API v1" });
  await expect(row).toBeVisible();
  await row.click();
  await page.getByRole("button", { name: "Request review" }).click();
  await expect(row.locator(".status")).toHaveText(/In review/);
  // The e2e owner is the organization's only owner, so they may approve their own version.
  await page.getByLabel("Review comment").fill("Looks good");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(row.locator(".status")).toHaveText("Approved");
  await expect(page.locator(".review-history")).toContainText("approved");
});

test("branch merge: merge a draft branch into main from the History panel", async ({ page }) => {
  const pid = await openNewProject(page, `Merge ${Date.now()}`);
  await api(page, `/api/projects/${pid}/ops`, { ops: [{ op: "add_node", node: { name: "Shop API", type: "service" } }] });
  await api(page, `/api/projects/${pid}/commit`, { message: "start", author: "e2e" });
  await api(page, `/api/projects/${pid}/branches`, { name: "draft/cache" });
  await api(page, `/api/projects/${pid}/ops?branch=draft%2Fcache`, { ops: [
    { op: "add_node", node: { name: "Cart Cache", type: "cache", technology: "Redis" } },
    { op: "add_edge", edge: { source: "Shop API", target: "Cart Cache" } }] });
  await page.reload();
  await expect(node(page, "Shop API")).toBeVisible();
  await expect(node(page, "Cart Cache")).toHaveCount(0);

  await tab(page, "History");
  await page.getByRole("button", { name: /Branches/ }).click();
  await page.locator(".branch-list li").filter({ hasText: "draft/cache" }).getByRole("button", { name: "Merge into…" }).click();
  const dialog = page.getByRole("dialog", { name: "Merge branches" });
  await dialog.getByRole("combobox", { name: "Target branch" }).click();
  await page.getByRole("listbox", { name: "Target branch" }).getByRole("option", { name: "main", exact: true }).click();
  await expect(dialog.locator(".merge-changes")).toContainText("Cart Cache");
  await dialog.getByRole("button", { name: "Merge into main" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(node(page, "Cart Cache")).toBeVisible();
});

test("exports: Markdown spec download and CLAUDE.md / AGENTS.md built from the model", async ({ page }) => {
  await openNewProject(page, `Export ${Date.now()}`, "ai-customer-support");
  await openTool(page, /Export spec/);
  const dialog = page.getByRole("dialog", { name: "Export specification" });
  const [download] = await Promise.all([page.waitForEvent("download"), dialog.getByRole("button", { name: "Markdown spec" }).click()]);
  expect(download.suggestedFilename()).toMatch(/\.md$/);
  const generate = dialog.getByRole("button", { name: /^(Generate|Regenerate)$/ });
  if (await generate.isVisible()) await generate.click();
  await expect(dialog.getByRole("button", { name: /Download CLAUDE\.md/ })).toBeEnabled();
  await expect(dialog).toContainText(/Order Service|Support Agent|Knowledge Base/);
  await dialog.getByRole("button", { name: "AGENTS.md" }).click();
  await expect(dialog.getByRole("button", { name: /Download AGENTS\.md/ })).toBeEnabled();
});

test("diagrams: C4, sequence and data-flow diagrams render from the model", async ({ page }) => {
  await openNewProject(page, `Diagrams ${Date.now()}`, "ai-customer-support");
  await openTool(page, /Diagrams/);
  const dialog = page.getByRole("dialog", { name: "Diagrams" });
  const types = dialog.getByLabel("Diagram types");
  await expect(types).toBeVisible();
  await expect(dialog.locator("svg[aria-label]").first()).toBeVisible({ timeout: 20_000 });
  for (const kind of [/Sequence/i, /Data flow/i]) {
    const pick = types.getByRole("button", { name: kind }).or(types.getByRole("tab", { name: kind })).first();
    if (await pick.count()) {
      await pick.click();
      await expect(dialog.locator("svg[aria-label]").first()).toBeVisible({ timeout: 20_000 });
    }
  }
});
