import { expect, test } from "@playwright/test";
import { assistant, node, openNewProject, send, tab } from "./helpers";

test("edit the canvas from the assistant, the inspector and requirements", async ({ page }) => {
  await openNewProject(page, `Editing ${Date.now()}`);

  // Assistant, Edit mode (offline parser without AI; the agent with AI)
  await assistant(page, "Edit");
  await send(page, "add Session Cache");
  await expect(node(page, "Session Cache")).toBeVisible();
  await send(page, "add Orders API connected to Session Cache");
  await expect(node(page, "Orders API")).toBeVisible();
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);
  await send(page, "rename Session Cache to Hot Cache");
  await expect(node(page, "Hot Cache")).toBeVisible();
  await expect(node(page, "Session Cache")).toHaveCount(0);

  // Inspector: select the component and change its technology
  await node(page, "Hot Cache").click();
  const tech = page.locator(".rightbar").getByLabel(/Technology/i).first();
  await tech.fill("Redis");
  await tech.press("Enter");
  await expect(node(page, "Hot Cache")).toContainText("Redis");

  // Requirements: add one, edit it, and see it is not traced to any component yet
  await tab(page, "Requirements");
  await page.getByRole("button", { name: "+ Functional" }).click();
  await page.locator(".req-row").first().click();
  const title = page.locator(".req-edit").getByLabel("Title");
  await title.fill("Customers can look up an order");
  await title.press("Enter");
  await expect(page.locator(".req-title")).toHaveText("Customers can look up an order");
  await expect(page.getByRole("tab", { name: /Not traced/ })).toContainText("1");

  // Remove, through the assistant again (docked left, it is the first tab)
  await tab(page, "Assistant");
  await send(page, "remove Hot Cache");
  await expect(node(page, "Hot Cache")).toHaveCount(0);
});

test("the workspace bar leads back to the projects and to main", async ({ page }) => {
  const pid = await openNewProject(page, `Nav ${Date.now()}`);
  await page.goto(`/#/p/${pid}/main`);
  await page.getByRole("button", { name: "New branch" }).click();
  await page.getByLabel("New branch name").fill("experiment/nav");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("combobox", { name: "Branch", exact: true })).toHaveAttribute("data-value", "experiment/nav");
  await page.getByRole("button", { name: "main", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Branch", exact: true })).toHaveAttribute("data-value", "main");
  await page.getByRole("button", { name: "Back to all projects" }).click();
  await expect(page.getByRole("button", { name: "+ New project" }).first()).toBeVisible();
});
