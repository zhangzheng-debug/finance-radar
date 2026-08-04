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

function integerArgument(name, fallback) {
  const value = Number(argument(name, String(fallback)));
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`--${name} must be a non-negative integer`);
  }
  return value;
}

(async () => {
  const url = argument("url", "https://radar.18-208-34-152.sslip.io:8443/radar/");
  const output = path.resolve(argument("output", "reports/ui_qa_latest/page.png"));
  const diagnosticsOutput = path.resolve(
    argument("diagnostics", output.replace(/\.png$/i, ".json"))
  );
  const tab = argument("tab");
  const waitMs = integerArgument("wait-ms", 2500);
  const width = integerArgument("width", 1366);
  const height = integerArgument("height", 768);
  const scrollY = integerArgument("scroll-y", 0);
  const fullPage = argument("full-page", "false").toLowerCase() === "true";

  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  const page = await browser.newPage({
    viewport: { width, height },
    deviceScaleFactor: 1,
    colorScheme: "dark",
  });
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

  let navigationError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
      navigationError = null;
      break;
    } catch (error) {
      navigationError = error;
      if (attempt < 3) await page.waitForTimeout(2000 * attempt);
    }
  }
  if (navigationError) throw navigationError;

  await page.locator('[data-testid="stAppViewContainer"]').waitFor({ timeout: 45000 });
  if (tab) {
    await page.getByText(tab, { exact: true }).first().click({ timeout: 30000 });
  }

  // Streamlit briefly has zero skeletons before the first script run begins.
  // Require substantive body content as well, otherwise that transient state
  // produces an attractive but false empty-page screenshot.
  await page.waitForTimeout(1000);
  await page.waitForFunction(
    () =>
      document.body.innerText.length > 200 &&
      document.querySelectorAll('[data-testid="stSkeleton"]').length === 0,
    null,
    { timeout: 90000 }
  );
  await page.waitForTimeout(waitMs);
  if (scrollY) {
    await page.mouse.move(Math.floor(width / 2), Math.floor(height / 2));
    await page.mouse.wheel(0, scrollY);
    await page.waitForTimeout(1000);
  }

  fs.mkdirSync(path.dirname(output), { recursive: true });
  await page.screenshot({ path: output, fullPage });

  const bodyText = await page.locator("body").innerText();
  const diagnostics = {
    captured_at_utc: new Date().toISOString(),
    requested_url: url,
    final_url: page.url(),
    title: await page.title(),
    viewport: { width, height },
    full_page: fullPage,
    scroll_y: scrollY,
    window_scroll_y: await page.evaluate(() => window.scrollY),
    tab: tab || null,
    canvas_count: await page.locator("canvas").count(),
    iframe_count: await page.locator("iframe").count(),
    dataframe_count: await page.locator('[data-testid="stDataFrame"]').count(),
    skeleton_count: await page.locator('[data-testid="stSkeleton"]').count(),
    alert_count: await page.locator('[role="alert"]').count(),
    body_text_length: bodyText.length,
    body_text_excerpt: bodyText.slice(0, 1000),
    console_errors: consoleErrors,
    page_errors: pageErrors,
    http_errors: httpErrors,
    screenshot: output,
  };
  fs.writeFileSync(diagnosticsOutput, `${JSON.stringify(diagnostics, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(diagnostics, null, 2)}\n`);

  await browser.close();
  if (diagnostics.skeleton_count > 0 || diagnostics.page_errors.length > 0) {
    process.exitCode = 2;
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
