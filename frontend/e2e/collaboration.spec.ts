import { expect, test, type Browser, type Page } from "@playwright/test";
import { assistant, node, openNewProject, send, tab } from "./helpers";
import { OWNER } from "./global-setup";

const DEV = { name: "Dana Developer", password: "another horse battery staple" };
let devSeq = 0;

/** A second person: their own browser context, signed up and joined to the owner's organization by invite. */
async function secondUser(browser: Browser, owner: Page, pid: string, name = DEV.name): Promise<Page> {
  const origin = new URL(owner.url()).origin;
  const { project } = await (await owner.request.get(`/api/projects/${pid}`)).json();
  const inv = await owner.request.post(`/api/orgs/${project.org_id}/invites`, { data: { role: "developer" }, headers: { Origin: origin } });
  expect(inv.ok(), await inv.text()).toBeTruthy();
  const { token } = await inv.json();

  const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] }, viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const headers = { Origin: origin };
  const su = await page.request.post(`${origin}/api/auth/signup`, { data: { ...DEV, name, email: `dev-${Date.now()}-${++devSeq}@e2e.test` }, headers });
  expect(su.ok(), await su.text()).toBeTruthy();
  const acc = await page.request.post(`${origin}/api/invites/${token}/accept`, { headers });
  expect(acc.ok(), await acc.text()).toBeTruthy();
  await page.goto(`${origin}/#/p/${pid}/main`);
  await expect(page.locator(".react-flow")).toBeVisible();
  return page;
}


test("two people edit the same canvas live, see each other online, and share version history", async ({ page: olivia, browser }) => {
  const pid = await openNewProject(olivia, `Collab ${Date.now()}`);
  const dana = await secondUser(browser, olivia, pid);

  // Who is online: each sees the other's avatar
  await expect(olivia.locator(`.presence .avatar[title="${DEV.name}"]`)).toBeVisible();
  await expect(dana.locator(`.presence .avatar[title="${OWNER.name}"]`)).toBeVisible();

  // Olivia adds a component; Dana sees it without reloading
  await assistant(olivia, "Edit");
  await send(olivia, "add Orders API");
  await expect(node(dana, "Orders API")).toBeVisible();

  // Dana edits Olivia's component; Olivia sees the change
  await assistant(dana, "Edit");
  await send(dana, "rename Orders API to Checkout API");
  await expect(node(olivia, "Checkout API")).toBeVisible();
  await expect(node(olivia, "Orders API")).toHaveCount(0);

  // Concurrent edits from both sides merge instead of overwriting each other
  await Promise.all([send(olivia, "add Payments Service"), send(dana, "add Inventory DB")]);
  for (const p of [olivia, dana]) {
    await expect(node(p, "Payments Service")).toBeVisible();
    await expect(node(p, "Inventory DB")).toBeVisible();
    await expect(node(p, "Checkout API")).toBeVisible();
  }

  // Selecting a component shows the other person who has it selected
  await node(dana, "Checkout API").click();
  await expect(node(olivia, "Checkout API").locator(".an-remote")).toHaveText(DEV.name);

  // Saved: after the store debounce, a fresh load (and the API's copy) has everyone's edits
  await expect.poll(async () => {
    const { state } = await (await olivia.request.get(`/api/projects/${pid}?branch=main`)).json();
    return Object.values(state.nodes as Record<string, { name: string }>).map((n) => n.name).sort();
  }, { timeout: 15_000 }).toEqual(["Checkout API", "Inventory DB", "Payments Service"]);
  await dana.reload();
  await expect(node(dana, "Inventory DB")).toBeVisible();
  await expect(node(dana, "Payments Service")).toBeVisible();

  // Version history: Olivia saves a version, Dana's History panel lists it live
  await tab(dana, "History");
  await tab(olivia, "History");
  await olivia.getByLabel("Version description").fill("Checkout flow sketch");
  await olivia.getByRole("button", { name: "Save version" }).click();
  await expect(dana.locator(".version-list")).toContainText("Checkout flow sketch");

  // …and after more edits, Dana can restore it and Olivia's canvas follows
  await tab(olivia, "Assistant");
  await send(olivia, "remove Inventory DB");
  await expect(node(dana, "Inventory DB")).toHaveCount(0);
  await dana.locator(".version-list li").filter({ hasText: "Checkout flow sketch" }).locator(".version-row").click();
  await dana.getByRole("button", { name: "Restore", exact: true }).click();
  await dana.getByRole("button", { name: /^Yes, restore v\d+$/ }).click();
  await expect(node(olivia, "Inventory DB")).toBeVisible();
  await expect(node(dana, "Inventory DB")).toBeVisible();

  await dana.context().close();
});

test("comments with @mentions, notifications, replies and resolving between two people", async ({ page: olivia, browser }) => {
  const pid = await openNewProject(olivia, `Comments ${Date.now()}`);
  await assistant(olivia, "Edit");
  await send(olivia, "add Orders API");
  const SAM = "Sam Second";
  const dana = await secondUser(browser, olivia, pid, SAM);
  await expect(node(dana, "Orders API")).toBeVisible();

  // Olivia selects the component and mentions Dana in a comment on it
  await node(olivia, "Orders API").click();
  await tab(olivia, "Comments");
  const box = olivia.getByLabel("New comment");
  await box.pressSequentially("@Sa");
  await expect(olivia.getByRole("listbox", { name: "People to mention" })).toContainText(SAM);
  await box.press("Enter");
  await box.pressSequentially("can you check the timeouts?");
  await expect(box).toHaveValue(`@${SAM} can you check the timeouts?`);
  await olivia.getByRole("button", { name: "Comment", exact: true }).click();
  await expect(olivia.locator(".thread")).toContainText("can you check the timeouts?");
  await expect(olivia.locator(".thread .mention")).toHaveText(`@${SAM}`);

  // Dana sees it live: a badge on the component, and a notification
  await expect(node(dana, "Orders API").locator(".an-comments")).toHaveText("1");
  const bell = dana.getByRole("button", { name: /^Notifications/ });
  await dana.reload(); // the bell polls; a reload checks it straight away
  await expect(bell).toHaveAccessibleName("Notifications, 1 unread");
  await bell.click();
  await dana.getByRole("menuitem", { name: /mentioned you/ }).click();
  await expect(dana.locator(".thread.focused")).toContainText("can you check the timeouts?");
  await expect(bell).toHaveAccessibleName("Notifications");

  // Dana replies; Olivia sees the reply live and gets a reply notification
  await dana.locator(".thread.focused").getByRole("button", { name: "Reply" }).click();
  await dana.getByLabel("Reply").fill("30s on reads, 5s on writes");
  await dana.locator(".thread.focused").getByRole("button", { name: "Reply", exact: true }).click();
  await expect(olivia.locator(".thread .reply")).toContainText("30s on reads, 5s on writes");
  await expect(olivia.getByRole("button", { name: "Notifications, 1 unread" })).toBeVisible();

  // Olivia resolves the thread: it leaves the open list and the badge for both
  await olivia.locator(".thread").getByRole("button", { name: "Resolve", exact: true }).click();
  await expect(olivia.locator(".thread")).toHaveCount(0);
  await expect(node(dana, "Orders API").locator(".an-comments")).toHaveCount(0);
  await dana.getByRole("tab", { name: /Resolved · 1/ }).click();
  await expect(dana.locator(".thread")).toContainText(`Resolved by ${OWNER.name}`);

  await dana.context().close();
});
