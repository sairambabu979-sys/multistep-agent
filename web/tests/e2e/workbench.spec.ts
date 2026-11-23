import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const tokens = () =>
  JSON.parse(readFileSync(join(__dirname, "..", "..", ".e2e-state", "e2e-tokens.json"), "utf-8")) as {
    approver: string;
    viewer: string;
  };

async function signIn(page: Page, who: "approver" | "viewer") {
  await page.goto("/");
  await page.getByLabel("Token").fill(tokens()[who]);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Mission Control" })).toBeVisible();
}

async function noSeriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

test("a wrong token is refused, and says what to do", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Token").fill("msa_not-a-token");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "token" })).toHaveText(
    "That token is not valid (msa tokens add issues one).",
  );
});

test("Mission Control shows every run and what waits on a person", async ({ page }) => {
  await signIn(page, "viewer");
  const waiting = page.getByRole("table", { name: "Runs waiting on a person" });
  await expect(waiting.getByRole("link", { name: "release-1" })).toBeVisible();
  await expect(waiting.getByRole("link", { name: "ruling-1" })).toBeVisible();
  const history = page.getByRole("table", { name: "All runs, newest first" });
  await expect(history.getByRole("link", { name: "done-1" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Start a simulated run" })).toHaveCount(0); // viewers cannot
  await noSeriousViolations(page);
});

test("a viewer can follow a run but not approve it", async ({ page }) => {
  await signIn(page, "viewer");
  await page.goto("/runs/release-1");
  await expect(page.getByText("An approver must answer this release gate.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve and publish" })).toHaveCount(0);
});

test("an approver approves a release and the run publishes", async ({ page }) => {
  await signIn(page, "approver");
  await page.goto("/runs/release-1");
  await expect(page.getByRole("progressbar", { name: "Run progress" })).toHaveAttribute(
    "aria-valuetext",
    /Stage 14 of 15: Human approval/,
  );
  await noSeriousViolations(page);
  await page.getByRole("button", { name: "Approve and publish" }).click();
  await expect(page.getByRole("heading", { name: "release-1" }).locator("..").getByText("Completed")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("tab", { name: "Timeline" }).click();
  await expect(page.getByText("release_approved")).toBeVisible();
  await expect(page.getByText(/by: grace/).first()).toBeVisible();
  await page.getByRole("tab", { name: "Workflow" }).click();
  await expect(page.getByText(/Path: Created → .*Completed/)).toBeVisible();
});

test("a dispute is ruled on from the Approval Center", async ({ page }) => {
  await signIn(page, "approver");
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Approval Center" }).click();
  const card = page.locator("section", { has: page.getByRole("link", { name: "ruling-1" }) });
  await card.getByRole("combobox").selectOption("dismissed");
  await card.getByRole("button", { name: "Rule" }).click();
  await expect(page.locator("section", { has: page.getByRole("link", { name: "ruling-1" }) })).toHaveCount(0, {
    timeout: 15_000,
  }); // ruled: the run moved on, and nothing waits on a person for it now
  await noSeriousViolations(page);
});

test("the run views are reachable from the keyboard", async ({ page }) => {
  await signIn(page, "viewer");
  await page.goto("/runs/done-1");
  const overview = page.getByRole("tab", { name: "Overview" });
  await overview.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Timeline" })).toBeFocused();
  await expect(page.getByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(page.getByRole("tab", { name: "Pull request" })).toBeFocused();
  await page.goto("/runs/done-1");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
});

test("the theme follows the choice, survives a reload, and stays accessible", async ({ page }) => {
  await signIn(page, "viewer");
  await page.getByLabel("Theme").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await noSeriousViolations(page);
  for (const path of ["/metrics", "/preferences", "/approvals"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await noSeriousViolations(page);
  }
  await page.getByLabel("Theme").selectOption("light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await noSeriousViolations(page);
});

test("metrics come from the runs' own states", async ({ page }) => {
  await signIn(page, "viewer");
  await page.goto("/metrics");
  const runs = page.locator("dt", { hasText: /^Runs$/ }).locator("..").locator("dd");
  await expect(runs).toHaveText("3");
});
