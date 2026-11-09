import { expect, type Page } from "@playwright/test";

export const realAI = process.env.E2E_REAL_AI === "1";

/** Create a project through the API (fast) and open its workspace. */
export async function openNewProject(page: Page, name: string, template = "blank"): Promise<string> {
  const origin = new URL(page.context()["_options"]?.baseURL ?? "http://localhost:5174").origin;
  const r = await page.request.post("/api/projects", { data: { name, template }, headers: { Origin: origin } });
  expect(r.ok(), await r.text()).toBeTruthy();
  const { id } = await r.json();
  await page.goto(`/#/p/${id}/main`);
  await expect(page.getByRole("button", { name: "Back to all projects" })).toBeVisible();
  await expect(page.locator(".react-flow")).toBeVisible();
  return id as string;
}

/** A component on the canvas, by its visible name. */
export const node = (page: Page, name: string) => page.locator(".react-flow__node").filter({ hasText: name });

/** The assistant panel and its mode buttons. */
export async function assistant(page: Page, mode: "Design" | "Edit" | "Ask") {
  const panel = page.locator(".command-bar");
  await panel.getByRole("tablist", { name: "Assistant mode" }).getByRole("tab", { name: mode, exact: true }).click();
  return panel;
}

/** Send a message in the assistant's current mode and wait for the reply to finish. */
export async function send(page: Page, text: string) {
  const panel = page.locator(".command-bar");
  await panel.locator(".cmd-form input").fill(text);
  await panel.locator(".cmd-form button.primary").click();
  await expect(panel.locator(".cmd-entry.you").filter({ hasText: text }).first()).toBeVisible();
  await expect(panel.getByText(/AI is editing the canvas…|Thinking…/)).toHaveCount(0);
}

/** Open a sidebar panel by its tab name. */
export async function tab(page: Page, name: "Requirements" | "Health" | "History" | "Decisions" | "Assistant") {
  await page.locator(".side-tabs").getByRole("tab", { name: new RegExp(name) }).click();
}

export async function openTool(page: Page, item: RegExp) {
  await page.getByRole("button", { name: "Tools", exact: true }).click();
  await page.getByRole("menuitem", { name: item }).click();
}
