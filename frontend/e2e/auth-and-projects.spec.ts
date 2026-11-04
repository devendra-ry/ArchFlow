import { expect, test } from "@playwright/test";

test.describe("sign up and projects", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a new person signs up, creates a project from a template and finds it again", async ({ page }) => {
    await page.goto("/#/signup");
    await page.getByLabel("Email").fill(`maya-${Date.now()}@e2e.test`);
    await page.getByLabel(/Your name/).fill("Maya Maker");
    await page.getByLabel(/Password/).fill("a long enough password");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByRole("heading", { level: 1, name: /Maya/ })).toBeVisible();

    await page.getByRole("button", { name: "+ New project" }).first().click();
    await page.getByLabel("Project name").fill("Support Bot");
    await page.getByRole("button", { name: /customer-support platform/i }).click();
    await page.getByRole("button", { name: "Create and open" }).click();

    await expect(page.locator(".react-flow__node").filter({ hasText: "WhatsApp" })).toBeVisible();
    await expect(page.locator(".side-tabs").getByRole("tab", { name: /Requirements/ })).toBeVisible();

    // The labelled way back
    await page.getByRole("button", { name: "Back to all projects" }).click();
    await expect(page.getByText("Support Bot").first()).toBeVisible();
  });

  test("a wrong password is refused", async ({ page }) => {
    await page.goto("/#/login");
    await page.getByLabel("Email").fill("owner@e2e.test");
    await page.getByLabel(/Password/).fill("not the password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
  });
});
