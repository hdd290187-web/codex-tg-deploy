import { chromium } from "/tmp/codex-deploy-playwright/node_modules/playwright/index.mjs";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import assert from "node:assert/strict";

const root = "/opt/codex-deploy";
const html = execFileSync(`${root}/.venv/bin/python`, ["-c", `
from jinja2 import Environment, FileSystemLoader, select_autoescape
env=Environment(loader=FileSystemLoader('${root}/deployment-center/codex_deploy/templates'),autoescape=select_autoescape())
print(env.get_template('index.html').render(prefix='/codex-deploy',csrf='fixture',latest_version='2.1.0',insecure=False,efforts=['low','medium','high','xhigh'],plan_efforts=['low','medium','high','xhigh'],recent=[]))
`], { encoding: "utf8" });
const base = "https://center.test/codex-deploy/";
const original = '\ufeff  Original TXT\r\n\t\u4e2d\u6587 "quoted" \\literal\r\n ';
await fs.mkdir(`${root}/tests/artifacts`, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: "/usr/bin/google-chrome", args: ["--no-sandbox"] });
try {
  for (const profile of [
    { name: "desktop", viewport: { width: 1440, height: 1000 } },
    { name: "mobile", viewport: { width: 390, height: 844 } },
  ]) {
    const context = await browser.newContext({ viewport: profile.viewport });
    const page = await context.newPage();
    const errors = [];
    const deployments = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("https://center.test/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/codex-deploy/") return route.fulfill({ contentType: "text/html", body: html });
      if (path.startsWith("/codex-deploy/static/")) {
        const file = path.split("/").at(-1);
        return route.fulfill({ contentType: file.endsWith(".css") ? "text/css" : "application/javascript",
          body: await fs.readFile(`${root}/deployment-center/codex_deploy/static/${file}`, "utf8") });
      }
      let data = {};
      if (path.endsWith("/telegram/verify")) data = { bot: { name: "Fixture", username: "fixture_bot", id: 1 } };
      else if (path.endsWith("/telegram/bind")) data = { bound: true, bot: { username: "fixture_bot" }, user_id: 7, chat_id: 9 };
      else if (path.endsWith("/api/deployments")) {
        deployments.push(route.request().postDataJSON());
        data = { id: "fixture", command: "fixture command", expires_at: Date.now() / 1000 + 60 };
      } else if (path.endsWith("/deployments/fixture")) data = { status: "SUCCESS", progress: [] };
      return route.fulfill({ json: data });
    });
    async function bind() {
      await page.locator("#bot-token").fill("fixture-token");
      await page.locator("#verify-bot").click();
      await page.waitForFunction(() => !document.getElementById("generate").disabled);
      assert.equal(await page.locator("#bot-token").inputValue(), "fixture-token");
    }
    await page.goto(base);
    await bind();
    await page.locator("#codex-pre-instructions").fill("  Multiline\nExact spaces  ");
    await page.locator("#codex-prompt-enabled").check();
    assert.match(await page.locator("#codex-prompt-count").textContent(), /\d+.*131072/);
    const layout = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth,
      width: document.documentElement.scrollWidth,
      textarea: document.getElementById("codex-pre-instructions").getBoundingClientRect().toJSON() }));
    assert.ok(layout.width <= layout.viewport + 1, `${profile.name} overflow`);
    assert.ok(layout.textarea.width > 0 && layout.textarea.right <= layout.viewport, `${profile.name} textarea overflow`);
    await page.screenshot({ path: `${root}/tests/artifacts/prompt-${profile.name}.png`, fullPage: true });
    await page.locator("#generate").click();
    await page.locator("#deployment").waitFor({ state: "visible" });
    assert.equal(deployments[0].codex_pre_instructions, "  Multiline\nExact spaces  ");
    assert.equal(deployments[0].codex_pre_instructions_enabled, true);
    assert.equal(deployments[0].api_key, "");
    assert.equal(deployments[0].claude_api_key, "");

    await page.goto(base);
    await bind();
    await page.locator("#codex-prompt-file").setInputFiles({ name: "prompt.txt", mimeType: "text/plain", buffer: Buffer.from(original) });
    await page.waitForFunction(() => document.getElementById("codex-pre-instructions").value.includes("Original TXT"));
    assert.equal(await page.locator("#codex-prompt-enabled").isChecked(), false);
    await page.locator("#generate").click();
    await page.locator("#deployment").waitFor({ state: "visible" });
    assert.equal(deployments[1].codex_pre_instructions, original);
    assert.equal(deployments[1].codex_pre_instructions_enabled, false);

    await page.goto(base);
    await page.locator("#codex-pre-instructions").fill("Keep old content");
    await page.locator("#codex-prompt-file").setInputFiles({ name: "invalid.txt", mimeType: "text/plain", buffer: Buffer.from([0xff, 0x00]) });
    await page.waitForFunction(() => document.getElementById("form-error").textContent.length > 0);
    assert.equal(await page.locator("#codex-pre-instructions").inputValue(), "Keep old content");
    assert.deepEqual(errors, []);
    console.log(`${profile.name}: text, exact BOM/CRLF upload, disabled persistence, empty engine configs, invalid UTF-8 and layout passed`);
    await context.close();
  }
} finally {
  await browser.close();
}
