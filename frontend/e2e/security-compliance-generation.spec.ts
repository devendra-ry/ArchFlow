import { createHmac, randomBytes } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { node, openNewProject, openTool } from "./helpers";

/** RFC 6238 code for a base32 secret (what an authenticator app shows). */
function totp(secret: string, at = Date.now()): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secret.replace(/[\s=]/g, "").toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / 30)));
  const h = createHmac("sha1", key).update(counter).digest();
  const o = h[h.length - 1] & 0xf;
  return String(((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000)).padStart(6, "0");
}

/** A fresh account in its own browser context (two-step sign-in must not touch the shared owner session). */
async function freshUser(page: Page) {
  const origin = new URL(page.url() === "about:blank" ? "http://localhost:5174" : page.url()).origin;
  const user = { email: `mfa-${randomBytes(4).toString("hex")}@e2e.test`, name: "Mira Factor", password: randomBytes(16).toString("base64url") };
  const r = await page.request.post("/api/auth/signup", { data: user, headers: { Origin: origin } });
  expect(r.ok(), await r.text()).toBeTruthy();
  return user;
}

test("compliance: framework scores, a failing control, show it on the canvas and record evidence", async ({ page }) => {
  await openNewProject(page, `Compliance ${Date.now()}`, "ai-customer-support");
  await openTool(page, /Compliance/);
  const dialog = page.getByRole("dialog", { name: "Compliance" });
  await expect(dialog.locator(".cmp-score").first()).toContainText("%");
  await dialog.getByRole("group", { name: "Frameworks" }).getByText("GDPR").click();
  await expect(dialog.locator(".cmp-score")).toHaveCount(2);

  const control = dialog.locator(".cmp-control").filter({ hasText: "Encryption in transit" }).first();
  await control.locator(".cmp-row").click();
  await expect(control).toContainText("Fix");
  await control.getByRole("button", { name: "Show on canvas" }).click();
  await expect(dialog).toBeHidden();
  // The control's components are selected, and no stack of node toolbars covers them.
  await expect(page.locator(".react-flow__node.selected").first()).toBeVisible();
  expect(await page.locator(".react-flow__node.selected").count()).toBeGreaterThan(1);
  await expect(page.locator(".node-toolbar:visible")).toHaveCount(0);

  await openTool(page, /Compliance/);
  const evidence = dialog.locator(".cmp-control").filter({ hasText: "Encryption in transit" }).first();
  await expect(evidence.getByLabel("Evidence note")).toBeVisible();
  await evidence.getByRole("combobox", { name: "Evidence" }).click();
  await page.getByRole("option", { name: "Met by process" }).click();
  await evidence.getByLabel("Evidence note").fill("mTLS through the service mesh, see ticket SEC-12");
  await evidence.getByRole("button", { name: "Save" }).click();
  // A control met by process leaves "To fix" and counts as done.
  await dialog.getByRole("navigation", { name: "Filter controls" }).getByRole("button", { name: "Done" }).click();
  await expect(dialog.locator(".cmp-control").filter({ hasText: "Encryption in transit" }).first().locator(".cmp-chip").first())
    .toContainText(/Met/i);
});

test("generate: Terraform files pass their checks, switch cloud and format, download a zip", async ({ page }) => {
  await openNewProject(page, `Generate ${Date.now()}`, "ai-customer-support");
  await openTool(page, /Generate/);
  const dialog = page.getByRole("dialog", { name: "Generate files" });
  await expect(dialog.getByText(/all checks passed/)).toBeVisible();
  await expect(dialog.getByRole("tab", { name: /main\.tf/ })).toBeVisible();

  await dialog.getByRole("combobox", { name: "Cloud" }).click();
  await page.getByRole("option", { name: "Azure" }).click();
  await dialog.getByRole("tab", { name: /versions\.tf/ }).click();
  await expect(dialog.getByLabel(/versions\.tf preview/)).toContainText("azurerm");

  await dialog.getByRole("radio", { name: "Docker Compose" }).click();
  await expect(dialog.getByRole("tab", { name: /docker-compose\.ya?ml/ })).toBeVisible();
  await dialog.getByRole("navigation", { name: "What to generate" }).getByRole("button", { name: "Documents" }).click();
  await expect(dialog.getByText(/all checks passed/)).toBeVisible();

  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: /zip/i }).click();
  expect((await download).suggestedFilename()).toMatch(/\.zip$/);
});

test("project access: the owner sees everyone's role on the project", async ({ page }) => {
  await openNewProject(page, `Access ${Date.now()}`);
  await openTool(page, /Project access/);
  const dialog = page.getByRole("dialog", { name: /access/i });
  await expect(dialog.getByText("Olivia Owner")).toBeVisible();
  await expect(dialog.getByText(/owner/i).first()).toBeVisible();
});

test.describe("sign-in security", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("two-step sign-in: turn it on, sign in with a code, a wrong code is refused", async ({ page }) => {
    await page.goto("/");
    const user = await freshUser(page);
    await page.goto("/");
    await page.locator(".avatar-btn").click();
    await page.getByRole("menuitem", { name: /Two-step sign-in/ }).click();
    const dialog = page.getByRole("dialog", { name: "Two-step sign-in" });
    await dialog.getByRole("button", { name: "Set up two-step sign-in" }).click();
    const secret = (await dialog.locator(".mfa-secret").innerText()).replace(/\s/g, "");
    await dialog.getByLabel("Code from your app").fill(totp(secret));
    await dialog.getByRole("button", { name: "Turn on" }).click();
    await expect(dialog.getByText(/On\./)).toBeVisible();
    await expect(dialog.locator(".mfa-codes, .recovery-codes, li").first()).toBeVisible();

    // Sign out and back in: the password alone is not enough any more.
    await page.request.post("/api/auth/logout", { headers: { Origin: new URL(page.url()).origin } });
    await page.goto("/");
    await page.getByRole("button", { name: "Sign in" }).first().click();
    await page.getByLabel("Email").fill(user.email);
    await page.getByLabel("Password").fill(user.password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    const code = page.locator("input[autocomplete='one-time-code']");
    await expect(code).toBeVisible();
    await code.fill("000000");
    await page.getByRole("button", { name: /Verify|Continue|Sign in/ }).last().click();
    await expect(page.getByRole("alert")).toBeVisible();
    await code.fill(totp(secret, Date.now() + 30_000)); // the next step: the enrolment code cannot be reused
    await page.getByRole("button", { name: /Verify|Continue|Sign in/ }).last().click();
    await expect(page.locator(".avatar-btn")).toBeVisible();
  });

  test("single sign-on: an email domain without SSO is told to use a password", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Sign in" }).first().click();
    await page.getByRole("button", { name: "Sign in with SSO" }).click();
    await page.getByLabel("Work email").fill("someone@no-sso.e2e.test");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText(/not set up for this email domain/)).toBeVisible();
    await page.getByRole("button", { name: /Use a password instead/ }).click();
    await expect(page.getByLabel("Password")).toBeVisible();
  });
});

test.describe("phone", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("the workspace fits a phone: no sideways scroll, canvas actions, panels in a drawer", async ({ page }) => {
    // On a phone the way back to the projects lives in the ⋯ menu, so open the project directly.
    const r = await page.request.post("/api/projects", { data: { name: `Phone ${Date.now()}`, template: "ai-customer-support" },
      headers: { Origin: "http://localhost:5174" } });
    await page.goto(`/#/p/${(await r.json()).id}/main`);
    await expect(node(page, "API Gateway")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
    const bar = page.getByRole("toolbar", { name: "Canvas actions" });
    await expect(bar).toBeVisible();
    await bar.getByRole("button", { name: "Show panels" }).click();
    await expect(page.locator(".side-tabs")).toBeVisible();
  });
});
