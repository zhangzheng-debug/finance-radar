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
  await page.waitForFunction(
    () =>
      document.body.innerText.length > 200 &&
      document.querySelectorAll('[data-testid="stSkeleton"]').length === 0,
    null,
    { timeout: 90000 }
  );
  await page.waitForTimeout(1200);
}

function luminance(hex) {
  const rgb = hex
    .replace("#", "")
    .match(/.{2}/g)
    .map((value) => parseInt(value, 16) / 255)
    .map((value) => (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4));
  return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
}

function contrast(foreground, background) {
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

const tokenPairs = [
  ["primary text / canvas", "#e6eef5", "#060c13"],
  ["muted text / canvas", "#879caf", "#060c13"],
  ["muted text / panel", "#879caf", "#0a1420"],
  ["cyan action / panel", "#29bde3", "#0a1420"],
  ["green status / panel", "#3ed59f", "#0a1420"],
  ["amber status / panel", "#f0b35a", "#0a1420"],
  ["red status / panel", "#ff6b7c", "#0a1420"],
  ["violet evidence / panel", "#9b8afb", "#0a1420"],
  ["safe banner / safe surface", "#a8c8d7", "#0a1d2a"],
];

async function domAudit(page) {
  return page.evaluate(() => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return (
        style.visibility !== "hidden" &&
        style.display !== "none" &&
        rect.width > 0 &&
        rect.height > 0
      );
    };
    const accessibleName = (element) => {
      const labelledBy = (element.getAttribute("aria-labelledby") || "")
        .split(/\s+/)
        .filter(Boolean)
        .map((id) => document.getElementById(id)?.textContent || "")
        .join(" ");
      let labelText = "";
      if (element.id) {
        try {
          labelText = document.querySelector(`label[for="${CSS.escape(element.id)}"]`)?.textContent || "";
        } catch (_) {}
      }
      return (
        element.getAttribute("aria-label") ||
        labelledBy ||
        labelText ||
        element.getAttribute("alt") ||
        element.getAttribute("title") ||
        element.textContent ||
        element.getAttribute("placeholder") ||
        ""
      ).trim();
    };
    const interactiveSelector = [
      "button",
      "a[href]",
      "input:not([type=hidden])",
      "select",
      "textarea",
      '[role="button"]',
      '[role="tab"]',
      '[role="link"]',
      '[tabindex]:not([tabindex="-1"])',
    ].join(",");
    const interactive = [...document.querySelectorAll(interactiveSelector)].filter(visible);
    const missingNames = interactive
      .filter((element) => !accessibleName(element))
      .map((element) => ({ tag: element.tagName, role: element.getAttribute("role") }))
      .slice(0, 20);
    const tinyTargets = interactive
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          name: accessibleName(element).slice(0, 80),
          tag: element.tagName,
          width: Math.round(rect.width * 10) / 10,
          height: Math.round(rect.height * 10) / 10,
          offCanvas: rect.right <= 0 || rect.left >= window.innerWidth,
          inlineTextLink:
            element.tagName === "A" &&
            getComputedStyle(element).display === "inline" &&
            Boolean(element.closest("p,li")),
        };
      })
      .filter(
        (item) =>
          (item.width < 24 || item.height < 24) &&
          !item.inlineTextLink &&
          !item.offCanvas
      )
      .slice(0, 30);
    const ids = [...document.querySelectorAll("[id]")].map((element) => element.id);
    const duplicates = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
    const headings = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")]
      .filter(visible)
      .map((element) => ({ level: Number(element.tagName.slice(1)), text: element.textContent.trim().slice(0, 100) }));
    const headingSkips = [];
    for (let index = 1; index < headings.length; index += 1) {
      if (headings[index].level > headings[index - 1].level + 1) {
        headingSkips.push({ from: headings[index - 1], to: headings[index] });
      }
    }
    return {
      title: document.title,
      language: document.documentElement.lang || "",
      body_characters: document.body.innerText.length,
      main_landmarks: document.querySelectorAll('main,[role="main"]').length,
      navigation_landmarks: document.querySelectorAll('nav,[role="navigation"]').length,
      interactive_elements: interactive.length,
      missing_accessible_names: missingNames,
      tiny_non_inline_targets: tinyTargets,
      duplicate_ids: duplicates,
      images_without_alt: [...document.querySelectorAll("img")]
        .filter(visible)
        .filter((element) => !element.hasAttribute("alt"))
        .length,
      iframes_without_title: [...document.querySelectorAll("iframe")]
        .filter(visible)
        .filter((element) => !(element.getAttribute("title") || "").trim())
        .length,
      headings,
      heading_level_skips: headingSkips,
      horizontal_overflow_pixels: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
      skeletons: document.querySelectorAll('[data-testid="stSkeleton"]').length,
    };
  });
}

async function focusAudit(page) {
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  });
  const samples = [];
  const seen = new Set();
  for (let index = 0; index < 32; index += 1) {
    await page.keyboard.press("Tab");
    const sample = await page.evaluate(() => {
      const element = document.activeElement;
      if (!element || element === document.body) return null;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return {
        key: `${element.tagName}:${element.getAttribute("aria-label") || element.textContent || element.id}`.slice(0, 160),
        tag: element.tagName,
        name: (element.getAttribute("aria-label") || element.textContent || element.id || "").trim().slice(0, 100),
        outline_style: style.outlineStyle,
        outline_width: style.outlineWidth,
        box_shadow: style.boxShadow,
        width: Math.round(rect.width * 10) / 10,
        height: Math.round(rect.height * 10) / 10,
      };
    });
    if (!sample || seen.has(sample.key)) continue;
    seen.add(sample.key);
    const outlineWidth = parseFloat(sample.outline_width) || 0;
    sample.visible_indicator =
      (sample.outline_style !== "none" && outlineWidth >= 1) ||
      (sample.box_shadow && sample.box_shadow !== "none");
    samples.push(sample);
  }
  return {
    sampled: samples.length,
    visible_indicators: samples.filter((sample) => sample.visible_indicator).length,
    ratio: samples.length
      ? samples.filter((sample) => sample.visible_indicator).length / samples.length
      : 0,
    samples,
  };
}

(async () => {
  const baseUrl = argument(
    "base-url",
    "https://radar.18-208-34-152.sslip.io:8443/radar"
  ).replace(/\/$/, "");
  const output = path.resolve(
    argument("output", "reports/accessibility_public_latest.json")
  );
  const markdownOutput = output.replace(/\.json$/i, ".md");
  const scope = argument("scope", "public").trim().toLowerCase();
  const targetsByScope = {
    // These are the only pages intentionally reachable through the public
    // Nginx route.  A 404 for a management route is a security success, not an
    // accessibility failure.
    public: [
      ["Situation Room", `${baseUrl}/`],
      ["Replay Lab", `${baseUrl}/Replay_Lab`],
      ["Method and Boundaries", `${baseUrl}/Method_and_Boundaries`],
    ],
    // Run this scope only through the authenticated loopback tunnel, with
    // --base-url=http://127.0.0.1:18502/radar-admin (or its equivalent).
    admin: [
      ["Admin Overview", `${baseUrl}/`],
      ["Event Intelligence", `${baseUrl}/?_page=Event_Intelligence`],
      ["Operations and Model", `${baseUrl}/?_page=Operations_and_Model`],
      ["Adjudication Studio", `${baseUrl}/?_page=Adjudication_Studio`],
      ["Method and Boundaries", `${baseUrl}/?_page=Method_and_Boundaries`],
    ],
  };
  const targets = targetsByScope[scope];
  if (!targets) {
    throw new Error("--scope must be public or admin");
  }
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  const pages = [];
  try {
    for (const [name, url] of targets) {
      const page = await browser.newPage({
        viewport: { width: 1366, height: 768 },
        deviceScaleFactor: 1,
        colorScheme: "dark",
        reducedMotion: "reduce",
      });
      const pageErrors = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
      await waitForStreamlit(page);
      const desktop = await domAudit(page);
      const focus = await focusAudit(page);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(800);
      const mobile = await domAudit(page);
      const blockers = [];
      if (!desktop.title.trim()) blockers.push("missing_document_title");
      if (desktop.main_landmarks !== 1) blockers.push("main_landmark_count");
      if (desktop.navigation_landmarks !== 1) blockers.push("navigation_landmark_count");
      if (!desktop.headings.some((item) => item.level === 1)) blockers.push("missing_h1");
      if (desktop.missing_accessible_names.length) blockers.push("unnamed_interactive_controls");
      if (desktop.duplicate_ids.length) blockers.push("duplicate_ids");
      if (desktop.images_without_alt) blockers.push("images_without_alt");
      if (desktop.iframes_without_title) blockers.push("iframes_without_title");
      if (desktop.heading_level_skips.length) blockers.push("heading_level_skip");
      if (desktop.horizontal_overflow_pixels > 2 || mobile.horizontal_overflow_pixels > 2) {
        blockers.push("horizontal_overflow");
      }
      if (focus.sampled < 3 || focus.ratio < 0.8) blockers.push("keyboard_focus_visibility");
      const advisories = [];
      if (!/^zh(?:-|$)/i.test(desktop.language)) advisories.push("document_language_not_zh_CN");
      if (mobile.tiny_non_inline_targets.length) advisories.push("touch_targets_below_24px");
      pages.push({
        name,
        requested_url: url,
        final_url: page.url(),
        blockers,
        advisories,
        page_errors: pageErrors,
        desktop,
        mobile,
        focus,
      });
      await page.close();
    }
  } finally {
    await browser.close();
  }

  const contrastPairs = tokenPairs.map(([name, foreground, background]) => ({
    name,
    foreground,
    background,
    ratio: Math.round(contrast(foreground, background) * 100) / 100,
    wcag_aa_normal_text: contrast(foreground, background) >= 4.5,
  }));
  const blockerCount = pages.reduce((total, page) => total + page.blockers.length, 0);
  const errorCount = pages.reduce((total, page) => total + page.page_errors.length, 0);
  const advisoryCount = pages.reduce((total, page) => total + page.advisories.length, 0);
  const status = blockerCount || errorCount
    ? "FAIL"
    : advisoryCount
      ? "PASS_WITH_ADVISORIES"
      : "PASS";
  const report = {
    schema_version: 1,
    generated_at_utc: new Date().toISOString(),
    scope,
    target: baseUrl,
    status,
    limitations: "machine accessibility audit; not a substitute for assistive-technology user testing",
    thresholds: {
      one_main_landmark: true,
      one_navigation_landmark: true,
      one_h1_page_heading: true,
      all_interactive_controls_named: true,
      no_duplicate_ids: true,
      all_images_have_alt_attribute: true,
      all_visible_iframes_titled: true,
      no_heading_level_skips: true,
      no_horizontal_overflow_over_2px: true,
      focus_indicator_ratio_minimum: 0.8,
      wcag_aa_normal_text_contrast: 4.5,
      touch_target_advisory_minimum_px: 24,
    },
    blocker_count: blockerCount,
    advisory_count: advisoryCount,
    browser_page_error_count: errorCount,
    pages,
    color_token_contract: contrastPairs,
    contrast_failures: contrastPairs.filter((item) => !item.wcag_aa_normal_text).length,
  };
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  const lines = [
    "# Public accessibility machine audit",
    "",
    `- Status: **${report.status}**`,
    `- Pages: ${pages.length}/5`,
    `- Blocking findings: ${blockerCount}`,
    `- Browser page errors: ${errorCount}`,
    `- Color token contrast failures: ${report.contrast_failures}`,
    "",
    "| Page | Blockers | Advisories | Named controls | Focus indicators | Desktop/mobile overflow |",
    "|---|---:|---|---:|---:|---:|",
    ...pages.map(
      (item) =>
        `| ${item.name} | ${item.blockers.length} | ${item.advisories.join(", ") || "none"} | ` +
        `${item.desktop.interactive_elements - item.desktop.missing_accessible_names.length}/${item.desktop.interactive_elements} | ` +
        `${item.focus.visible_indicators}/${item.focus.sampled} | ` +
        `${item.desktop.horizontal_overflow_pixels}/${item.mobile.horizontal_overflow_pixels}px |`
    ),
    "",
    "This is deterministic browser evidence, not a claim of screen-reader user acceptance. A real assistive-technology review remains external.",
    "",
  ];
  fs.writeFileSync(markdownOutput, lines.join("\n"), "utf8");
  console.log(JSON.stringify(report, null, 2));
  process.exitCode = report.status === "FAIL" ? 1 : 0;
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
