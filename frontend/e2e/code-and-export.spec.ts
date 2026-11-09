import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { node, openNewProject, openTool } from "./helpers";

test("connect code: upload a code graph and reconstruct the architecture from it", async ({ page }) => {
  await openNewProject(page, `Code ${Date.now()}`);
  await openTool(page, /Code analysis/);
  const dialog = page.getByRole("dialog", { name: "Code analysis" });
  await expect(dialog).toBeVisible();

  // The graph was produced by `archflow-codegraph e2e/fixtures/shop` (docker-compose with an API, PostgreSQL, Redis).
  await dialog.getByText(/Private code\?/).click();
  await dialog.locator('input[type="file"]').setInputFiles("e2e/fixtures/shop-codegraph.json");
  await expect(dialog.getByText(/PostgreSQL/).first()).toBeVisible();

  await dialog.getByRole("button", { name: /Reconstruct architecture/ }).click();
  // Reconstruction opens a draft branch with the components found in the code.
  await expect(page.getByRole("combobox", { name: "Branch", exact: true })).toHaveAttribute("data-value", /^draft\//, { timeout: 30_000 });
  await page.keyboard.press("Escape");
  await expect(node(page, "PostgreSQL").or(node(page, "Postgres")).first()).toBeVisible();
  await expect(node(page, "Redis").first()).toBeVisible();
});

test("get the diagram: export PNG, SVG and the interactive HTML", async ({ page }) => {
  await openNewProject(page, `Export ${Date.now()}`, "ai-customer-support");
  for (const [kind, ext, label] of [["png", ".png", "PNG image"], ["svg", ".svg", "SVG image"], ["html", ".html", "Interactive diagram (HTML)"]] as const) {
    await page.getByRole("button", { name: "Tools", exact: true }).click();
    const download = page.waitForEvent("download");
    await page.getByRole("group", { name: "Export this diagram" }).getByRole("menuitem", { name: `Export as ${label}` }).click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(new RegExp(`\\${ext}$`));
    const path = await file.path();
    const bytes = readFileSync(path);
    expect(bytes.length).toBeGreaterThan(1000);
    if (kind === "svg") expect(bytes.toString("utf8")).toContain("<svg");
    if (kind === "html") expect(bytes.toString("utf8")).toMatch(/WhatsApp/);
  }
});
