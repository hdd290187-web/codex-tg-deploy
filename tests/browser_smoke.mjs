import { chromium } from "/tmp/codex-deploy-playwright/node_modules/playwright/index.mjs";
import fs from "node:fs/promises";

const base = "https://gpt.cevyo.com/codex-deploy/";
const password = process.env.CODEX_DEPLOY_BROWSER_PASSWORD;
if (!password) throw new Error("CODEX_DEPLOY_BROWSER_PASSWORD is required");

await fs.mkdir("/opt/codex-deploy/tests/artifacts", { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: "/usr/bin/google-chrome",
  args: ["--no-sandbox"],
});

for (const profile of [
  { name: "desktop", viewport: { width: 1440, height: 1000 } },
  { name: "mobile", viewport: { width: 390, height: 844 } },
]) {
  const context = await browser.newContext({ viewport: profile.viewport });
  const page = await context.newPage();
  const errors = [];
  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      !message.text().includes("static.cloudflareinsights.com") &&
      !message.text().startsWith("Failed to load resource:")
    ) {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (networkResponse) => {
    if (networkResponse.status() >= 400) {
      errors.push(`HTTP ${networkResponse.status()} ${networkResponse.url()}`);
    }
  });
  page.on("requestfailed", (request) => {
    if (!request.url().includes("static.cloudflareinsights.com")) {
      errors.push(`REQUEST FAILED ${request.url()} ${request.failure()?.errorText || ""}`);
    }
  });
  const response = await page.goto(base, { waitUntil: "networkidle" });
  if (!response || response.status() !== 200) throw new Error(`${profile.name}: login HTTP failure`);
  await page.locator("#password").fill(password);
  await Promise.all([
    page.waitForLoadState("networkidle"),
    page.locator("#login-form button").click(),
  ]);
  await page.locator("#base-url").waitFor({ state: "visible" });
  const layout = await page.evaluate(() => ({
    viewportWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    buttonHeights: [...document.querySelectorAll("button")]
      .filter((node) => node.getClientRects().length > 0)
      .map((node) => node.getBoundingClientRect().height),
    inputWidths: [...document.querySelectorAll("input,select")]
      .filter((node) => node.getClientRects().length > 0)
      .map((node) => node.getBoundingClientRect().width),
  }));
  if (layout.scrollWidth > layout.viewportWidth + 1) {
    throw new Error(`${profile.name}: horizontal overflow ${layout.scrollWidth}/${layout.viewportWidth}`);
  }
  if (layout.buttonHeights.some((height) => height < 34)) {
    throw new Error(`${profile.name}: undersized button`);
  }
  if (layout.inputWidths.some((width) => width <= 0 || width > layout.viewportWidth)) {
    throw new Error(`${profile.name}: invalid input width`);
  }
  await page.locator("#custom-plan").check();
  if (!(await page.locator("#plan-row").isVisible())) throw new Error(`${profile.name}: plan control did not open`);
  await page.screenshot({
    path: `/opt/codex-deploy/tests/artifacts/center-${profile.name}.png`,
    fullPage: true,
  });
  if (errors.length) throw new Error(`${profile.name}: browser errors: ${errors.join(" | ")}`);
  console.log(`${profile.name}: ok ${JSON.stringify(layout)}`);
  await context.close();
}

await browser.close();
