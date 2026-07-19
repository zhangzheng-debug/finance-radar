const fs = require("fs");
const path = require("path");
const { chromium } = require(
  "C:/Users/MR/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright"
);

function argument(name, fallback = "") {
  const prefix = `--${name}=`;
  const item = process.argv.slice(2).find((value) => value.startsWith(prefix));
  return item ? item.slice(prefix.length) : fallback;
}

async function waitForStreamlit(page) {
  await page.locator('[data-testid="stAppViewContainer"]').waitFor({ timeout: 45000 });
  await page.waitForTimeout(1000);
  try {
    await page.waitForFunction(
      () =>
        document.body.innerText.length > 200 &&
        document.querySelectorAll('[data-testid="stSkeleton"]').length === 0,
      null,
      { timeout: 90000 }
    );
  } catch (error) {
    const diagnostic = await page.evaluate(() => ({
      url: window.location.href,
      body_text_length: document.body.innerText.length,
      skeleton_count: document.querySelectorAll('[data-testid="stSkeleton"]').length,
      body_preview: document.body.innerText.slice(0, 500),
    }));
    throw new Error(`Streamlit did not settle: ${JSON.stringify(diagnostic)}; ${error.message}`);
  }
}

function eventIdFrom(url) {
  return new URL(url).searchParams.get("event_id");
}

function recordCheck(checks, name, passed, detail) {
  checks.push({ name, passed: Boolean(passed), detail });
}

const baseUrl = argument(
  "base-url",
  "https://radar.167-172-69-16.sslip.io:8443/radar"
).replace(/\/$/, "");
const outputDir = path.resolve(argument("output-dir", "reports/ui_qa_20260719"));
const jsonPath = path.join(outputDir, "public_interaction_acceptance.json");
const markdownPath = path.join(outputDir, "public_interaction_acceptance.md");
fs.mkdirSync(outputDir, { recursive: true });
fs.rmSync(jsonPath, { force: true });
fs.rmSync(markdownPath, { force: true });
let browser = null;

(async () => {
  browser = await chromium.launch({ headless: true, channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    colorScheme: "dark",
  });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  const httpErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) {
      httpErrors.push({ status: response.status(), url: response.url() });
    }
  });

  const checks = [];
  const eventUrl = `${baseUrl}/Event_Intelligence?flow=${encodeURIComponent("已核验")}&limit=25`;
  await page.goto(eventUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  await waitForStreamlit(page);
  await page.getByText("证据矩阵", { exact: true }).waitFor({ state: "visible", timeout: 60000 });
  const initialEventId = eventIdFrom(page.url());
  const selectedEventButton = page.locator('button[data-testid="stBaseButton-primary"]');
  const initialSelectedLabel = (await selectedEventButton.first().innerText()).trim();
  recordCheck(checks, "event_selected_on_load", Boolean(initialEventId), initialEventId);

  // Click neutral body space so the keyboard handler is tested outside an input.
  await page.getByText("Event Workbench", { exact: true }).first().click();
  await page.keyboard.press("j");
  await page.waitForFunction(
    (priorId) => new URL(window.location.href).searchParams.get("event_id") !== priorId,
    initialEventId,
    { timeout: 30000 }
  );
  await waitForStreamlit(page);
  await page.waitForFunction(
    (priorLabel) => {
      const button = document.querySelector('button[data-testid="stBaseButton-primary"]');
      return button && button.innerText.trim() !== priorLabel;
    },
    initialSelectedLabel,
    { timeout: 60000 }
  );
  await page.getByText("证据矩阵", { exact: true }).waitFor({ state: "visible", timeout: 60000 });
  const afterJEventId = eventIdFrom(page.url());
  const afterJSelectedLabel = (await selectedEventButton.first().innerText()).trim();
  recordCheck(
    checks,
    "keyboard_j_selects_next_event",
    Boolean(
      afterJEventId &&
      afterJEventId !== initialEventId &&
      afterJSelectedLabel !== initialSelectedLabel
    ),
    `${initialEventId} -> ${afterJEventId}; ${initialSelectedLabel} -> ${afterJSelectedLabel}`
  );

  await page.getByText("Event Workbench", { exact: true }).first().click();
  await page.keyboard.press("k");
  await page.waitForFunction(
    (priorId) => new URL(window.location.href).searchParams.get("event_id") !== priorId,
    afterJEventId,
    { timeout: 30000 }
  );
  await waitForStreamlit(page);
  await page.waitForFunction(
    (expectedLabel) => {
      const button = document.querySelector('button[data-testid="stBaseButton-primary"]');
      return button && button.innerText.trim() === expectedLabel;
    },
    initialSelectedLabel,
    { timeout: 60000 }
  );
  await page.getByText("证据矩阵", { exact: true }).waitFor({ state: "visible", timeout: 60000 });
  await page.waitForFunction(
    () =>
      document.body.innerText.includes("EVIDENCE AGENT") &&
      document.body.innerText.includes("事件身份") &&
      document.body.innerText.includes("判断维度"),
    null,
    { timeout: 60000 }
  );
  const afterKEventId = eventIdFrom(page.url());
  recordCheck(
    checks,
    "keyboard_k_selects_previous_event",
    afterKEventId === initialEventId,
    `${afterJEventId} -> ${afterKEventId}`
  );

  await page.getByText("Event Workbench", { exact: true }).first().click();
  await page.keyboard.press("/");
  const searchInput = page.getByLabel("全局检索", { exact: true });
  await searchInput.waitFor({ state: "visible", timeout: 10000 });
  const searchFocused = await searchInput.evaluate((element) => element === document.activeElement);
  recordCheck(checks, "slash_focuses_global_search", searchFocused, `focused=${searchFocused}`);
  await searchInput.press("Escape");

  const eventScreenshot = path.join(outputDir, "event_keyboard_after_jk_1920x1080.png");
  await page.screenshot({ path: eventScreenshot, fullPage: false });
  const eventText = await page.locator("body").innerText();

  const replayUrl = `${baseUrl}/Replay_Lab`;
  await page.goto(replayUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  await waitForStreamlit(page);
  await page.getByRole("button", { name: "Run frozen replay", exact: true }).click();
  await page.waitForFunction(
    () => document.body.innerText.includes("1/2") && document.body.innerText.includes("PENDING"),
    null,
    { timeout: 60000 }
  );
  const firstStepText = await page.locator("body").innerText();
  const runIdMatch = firstStepText.match(/run_id=([^\s·]+)/);
  const runId = runIdMatch ? runIdMatch[1] : null;
  recordCheck(
    checks,
    "replay_starts_at_first_step",
    Boolean(runId && firstStepText.includes("1/2") && firstStepText.includes("PENDING")),
    `run_id=${runId || "missing"}`
  );

  await page.getByRole("button", { name: "Next step", exact: true }).click();
  await page.waitForFunction(
    () =>
      document.body.innerText.includes("2/2") &&
      document.body.innerText.includes("MET") &&
      document.body.innerText.includes("STEP 02"),
    null,
    { timeout: 60000 }
  );
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="stSkeleton"]').length === 0,
    null,
    { timeout: 30000 }
  );
  await page.waitForTimeout(1500);
  const completedText = await page.locator("body").innerText();
  recordCheck(
    checks,
    "replay_completes_with_expected_decision",
    completedText.includes("2/2") && completedText.includes("MET") && completedText.includes("RISK_REVIEW"),
    "progress=2/2 expectation=MET decision=RISK_REVIEW"
  );

  const replayScreenshot = path.join(outputDir, "replay_completed_1920x1080.png");
  await page.screenshot({ path: replayScreenshot, fullPage: false });
  const report = {
    accepted_at_utc: new Date().toISOString(),
    base_url: baseUrl,
    viewport: { width: 1920, height: 1080 },
    result:
      checks.every((check) => check.passed) &&
      consoleErrors.length === 0 &&
      pageErrors.length === 0 &&
      httpErrors.length === 0
        ? "PASS"
        : "FAIL",
    checks,
    event: {
      initial_event_id: initialEventId,
      after_j_event_id: afterJEventId,
      after_k_event_id: afterKEventId,
      screenshot: eventScreenshot,
      body_text_length: eventText.length,
    },
    replay: {
      run_id: runId,
      screenshot: replayScreenshot,
      body_text_length: completedText.length,
    },
    console_errors: consoleErrors,
    page_errors: pageErrors,
    http_errors: httpErrors,
  };
  fs.writeFileSync(jsonPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  const markdown = [
    "# Public UI interaction acceptance",
    "",
    `- Result: **${report.result}**`,
    `- Accepted at: \`${report.accepted_at_utc}\``,
    `- Endpoint: \`${report.base_url}\``,
    "- Browser: headless Google Chrome through Playwright",
    "- Viewport: `1920x1080`",
    `- Replay run: \`${report.replay.run_id}\``,
    "",
    "| Check | Result | Evidence |",
    "|---|---|---|",
    ...checks.map(
      (check) => `| \`${check.name}\` | ${check.passed ? "PASS" : "FAIL"} | ${check.detail} |`
    ),
    "",
    "The test validates both the URL transition and the visibly selected event row for J/K. The slash check validates the actual focused DOM element. Replay is advanced one evidence step at a time and accepted only after `STEP 02`, `2/2`, `MET`, and `RISK_REVIEW` are simultaneously visible.",
    "",
    `- Console errors: \`${consoleErrors.length}\``,
    `- Page errors: \`${pageErrors.length}\``,
    `- HTTP errors: \`${httpErrors.length}\``,
    "",
    "A PASS requires every interaction check plus zero console errors, zero page errors, and zero HTTP 4xx/5xx responses.",
    "",
  ].join("\n");
  fs.writeFileSync(markdownPath, markdown, "utf8");
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);

  await browser.close();
  browser = null;
  if (report.result !== "PASS") process.exitCode = 2;
})().catch((error) => {
  const failure = {
    accepted_at_utc: new Date().toISOString(),
    base_url: baseUrl,
    result: "FAIL",
    error: error.stack || String(error),
  };
  fs.writeFileSync(jsonPath, `${JSON.stringify(failure, null, 2)}\n`, "utf8");
  fs.writeFileSync(
    markdownPath,
    `# Public UI interaction acceptance\n\n- Result: **FAIL**\n- Endpoint: \`${baseUrl}\`\n- Error: \`${String(error.message || error).replace(/`/g, "'")}\`\n`,
    "utf8"
  );
  process.stderr.write(`${error.stack || error}\n`);
  if (browser) browser.close().catch(() => {});
  process.exit(1);
});
