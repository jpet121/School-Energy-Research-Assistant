const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright");

const origin = process.env.RETROFIT_TEST_ORIGIN || "http://127.0.0.1:8765";
const executablePath = process.env.CHROMIUM_EXECUTABLE;
const output = path.resolve(process.env.RETROFIT_BROWSER_EVIDENCE || "local-data/browser-evidence");
const checks = [];
const pass = (id, detail) => checks.push({ id, status: "PASS", detail });

function fixturePdf(lines) {
  const commands = "BT /F1 12 Tf 50 730 Td 18 TL\n"
    + lines.map((line) => "(" + line.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)") + ") Tj T*").join("\n")
    + "\nET\nBT /F1 10 Tf 50 15 Td (7.2) Tj ET\n";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${Buffer.byteLength(commands)} >>\nstream\n${commands}endstream`,
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xref = Buffer.byteLength(pdf);
  pdf += "xref\n0 6\n0000000000 65535 f \n";
  pdf += offsets.map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("");
  pdf += `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf, "ascii");
}

async function tabTo(page, control) {
  await control.waitFor({ state: "visible" });
  for (let steps = 0; steps < 60; steps++) {
    if (await control.evaluate((node) => node === document.activeElement)) {
      assert(await control.evaluate((node) => {
        const style = getComputedStyle(node);
        return node.matches(":focus-visible") && parseFloat(style.outlineWidth) >= 2;
      }));
      return;
    }
    await page.keyboard.press("Tab");
  }
  assert.fail("Keyboard could not reach " + control);
}

async function keyActivate(page, control) {
  await tabTo(page, control);
  await page.keyboard.press("Enter");
}

async function download(page, format, name, keyboard = false) {
  const selector = page.getByLabel("File format");
  const prepare = page.getByRole("button", { name: "Prepare download" });
  const deliver = page.getByRole("button", { name: "Download saved draft" });
  if (keyboard) {
    await tabTo(page, selector);
    await page.keyboard.press(format === "markdown" ? "Home" : "End");
    assert.equal(await selector.inputValue(), format);
    await keyActivate(page, prepare);
  } else {
    await selector.selectOption(format);
    await prepare.click();
  }
  const saved = page.waitForEvent("download");
  if (keyboard) await keyActivate(page, deliver);
  else await deliver.click();
  await (await saved).saveAs(path.join(output, name));
  await page.getByRole("dialog").waitFor({ state: "hidden" });
}

async function journey(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(25_000);
  const errors = [];
  const remote = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("request", (request) => { if (/^https?:/.test(request.url()) && !request.url().startsWith(origin + "/")) remote.push(request.url()); });
  await page.goto(origin);
  assert.equal((await context.request.get(origin + "/catalog")).status(), 401);
  pass("AC-001.2-N", "Unauthenticated request denied on the running loopback service.");
  const fixture = fixturePdf(["22.2% EUI and 21.3% cost", "Miami school", "Modeled package reductions", "Percent", "Excludes EBCx", "<script>window.sourceExecuted=true</script>"]);
  fs.writeFileSync(path.join(output, "illustrative-controls.pdf"), fixture);
  await page.getByLabel("PDF file").setInputFiles({ name: "illustrative-controls.pdf", mimeType: "application/pdf", buffer: fixture });
  await page.getByLabel("Document title").fill("Fixture retrofit research");
  await page.getByLabel("Publisher").fill("Browser engineering fixture");
  await page.getByLabel("Source URL").fill("https://example.org/retrofit-browser-fixture.pdf");
  await page.getByLabel("Publication date").fill("2025-01-01");
  await page.getByLabel("Research scope").fill("Qualified fixture data");
  await page.getByLabel("Permitted use").fill("Local public test fixture");
  await page.getByLabel("I verified that this is a public document.").check();
  await page.getByRole("button", { name: "Stage PDF" }).click();
  await page.getByText("PDF staged. Its original bytes and provenance have been saved.").waitFor();
  await page.getByRole("button", { name: "Open staged source" }).click();
  await page.getByRole("button", { name: "Extract PDF", exact: true }).click();
  await page.getByRole("heading", { name: "Extraction review" }).waitFor();
  await page.getByRole("button", { name: "View original page" }).click();
  await page.getByRole("img", { name: "Original PDF page 1" }).waitFor();
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "View original page");
  await page.getByLabel("Verified printed page label (optional)").fill("7.2");
  for (const input of await page.locator("#staging-content input[type=checkbox]").all()) await input.check();
  await page.getByRole("button", { name: "Add table row" }).click();
  await page.getByLabel("Cell spans").fill("1");
  await page.getByLabel("Row header spans").fill("2");
  await page.getByLabel("Column header spans").fill("3");
  await page.getByLabel("Unit spans").fill("4");
  await page.getByLabel("Mandatory note spans").fill("5");
  await page.getByLabel("Original-page comparison notes").fill("Checked fixture page and required note.");
  await page.getByRole("button", { name: "Save extraction review" }).click();
  await page.getByText("Extraction review saved.").waitFor();
  await page.getByRole("button", { name: "Back to sources" }).click();
  await page.getByRole("button", { name: "Prepare snapshot" }).click();
  await page.getByRole("button", { name: "Publish snapshot" }).click();
  await page.getByText("Snapshot published.", { exact: true }).waitFor();
  pass("J01", "Browser staged, extracted, viewed, reviewed and published a real local PDF fixture.");

  const research = page.getByRole("link", { name: "Research", exact: true });
  await keyActivate(page, research);
  await page.getByRole("heading", { name: "Research", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => document.activeElement.id), "research-heading");
  await tabTo(page, page.getByLabel("Research question"));
  await page.keyboard.type("22.2");
  await keyActivate(page, page.getByRole("button", { name: "Search evidence" }));
  const inspect = page.getByRole("button", { name: "Inspect evidence" }).first();
  await keyActivate(page, inspect);
  await page.getByRole("dialog", { name: "Fixture retrofit research" }).waitFor();
  await page.getByText("PDF page 1 · printed page 7.2", { exact: false }).first().waitFor();
  await keyActivate(page, page.getByRole("button", { name: /Qualifier.*Excludes EBCx/ }));
  await page.getByRole("img", { name: /Highlighted region: Excludes EBCx/ }).waitFor();
  const accessibility = await context.newCDPSession(page);
  const tree = await accessibility.send("Accessibility.getFullAXTree");
  const exposed = tree.nodes.filter((item) => !item.ignored);
  const roles = [
    ["dialog", /Fixture retrofit research/],
    ["button", /Qualifier.*Excludes EBCx/],
    ["image", /Highlighted region: Excludes EBCx/],
  ];
  for (const [role, name] of roles) {
    assert(exposed.some((item) => item.role?.value === role && name.test(item.name?.value ?? "")), `${role} ${name} is missing from the accessibility tree`);
  }
  await accessibility.detach();
  pass("AX_EVIDENCE", "Chrome exposed the evidence dialog, qualifier control and highlighted excerpt image with accessible names.");
  await page.screenshot({ path: path.join(output, "evidence-1440.png") });
  await page.keyboard.press("Tab");
  assert(await page.evaluate(() => !!document.activeElement.closest("dialog")));
  await page.keyboard.press("Shift+Tab");
  assert(await page.evaluate(() => !!document.activeElement.closest("dialog")));
  await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "Inspect evidence");
  pass("AC-022.1-P", "Keyboard navigation opened research and evidence, reached the qualifier, stayed within the dialog and returned focus on Escape.");

  await page.getByRole("button", { name: "Review hosted input" }).click();
  await page.getByRole("heading", { name: "Exact question" }).waitFor();
  assert(await page.getByRole("button", { name: "Confirm and request draft" }).isDisabled());
  await page.keyboard.press("Escape");
  pass("AC-010.3-N", "Exact hosted input reviewed with confirmation disabled and local work still available.");

  await keyActivate(page, inspect);
  await keyActivate(page, page.getByRole("button", { name: "Add qualified note to draft" }));
  await page.getByRole("heading", { name: "Drafts and export" }).waitFor();
  assert((await page.getByLabel("Draft text").inputValue()).includes("Excludes EBCx"));
  await download(page, "json", "draft-root.json", true);
  await download(page, "markdown", "draft-root.md", true);
  const root = JSON.parse(fs.readFileSync(path.join(output, "draft-root.json")));
  const markdown = fs.readFileSync(path.join(output, "draft-root.md"), "utf8");
  assert(root.draft && root.parent_revision_id === null && root.claims[0].qualifiers.includes("Excludes EBCx"));
  assert(markdown.includes("Parent revision: None (root)") && markdown.includes(root.revision_id));
  await page.getByLabel("Draft text").fill(root.text.replace("22.2%", "23.0%"));
  await page.getByRole("button", { name: "Save draft revision" }).click();
  await page.getByText("needs evidence review", { exact: true }).waitFor();
  await download(page, "json", "draft-child.json");
  const child = JSON.parse(fs.readFileSync(path.join(output, "draft-child.json")));
  assert.equal(child.parent_revision_id, root.revision_id);
  assert.equal(child.claims[0].review_status, "NEEDS_EVIDENCE_REVIEW");
  pass("VX-08/VX-13", "Actual browser downloads agree on lineage and flag edited source-associated text.");
  pass("KEYBOARD_DEFAULT", "Tab focus and Enter completed search, qualifier inspection, draft creation and both exports.");

  await zoomChecks();

  await page.getByRole("link", { name: "Sources", exact: true }).click();
  await page.getByRole("button", { name: "Withdraw edition" }).click();
  await page.getByLabel("Reason for withdrawal").fill("Browser test withdrawal");
  await page.getByRole("button", { name: "Confirm withdrawal" }).click();
  await page.getByRole("link", { name: "Drafts", exact: true }).click();
  await page.getByLabel("Saved revision").selectOption(root.revision_id);
  await download(page, "json", "draft-withdrawn.json");
  const historical = JSON.parse(fs.readFileSync(path.join(output, "draft-withdrawn.json")));
  assert.equal(historical.text, root.text);
  assert.equal(historical.revision_id, root.revision_id);
  assert.deepEqual(historical.claims, root.claims);
  assert.equal(historical.withdrawal_warnings[0].status, "HISTORICAL_UNVERIFIED");
  await research.click();
  await page.getByRole("button", { name: "Search evidence" }).click();
  await page.getByText("No eligible evidence matched this question.", { exact: false }).waitFor();
  assert.equal(await page.evaluate(() => Boolean(window.sourceExecuted)), false);
  pass("VX-16/VX-17", "Browser historical download retains saved content and mappings, and new lexical search excludes the withdrawn edition.");
  await page.reload();
  await page.getByRole("link", { name: "Drafts", exact: true }).click();
  await page.getByRole("article").filter({ hasText: "23.0% EUI and 21.3% cost" })
    .getByRole("button", { name: "Open saved draft" }).click();
  assert((await page.getByLabel("Draft text").inputValue()).includes("23.0%"));
  await page.setViewportSize({ width: 390, height: 900 });
  await page.screenshot({ path: path.join(output, "draft-390.png") });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  assert.deepEqual(errors, []);
  assert.deepEqual(remote, []);
  pass("BROWSER_LOCAL", "No browser page errors or remote page requests. Saved draft reopened after reload.");
  await context.close();
}

async function zoomChecks() {
  for (const factor of [2, 4]) {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), `retrofit-chrome-${factor}-`));
    fs.mkdirSync(path.join(profile, "Default"), { recursive: true });
    fs.writeFileSync(path.join(profile, "Default", "Preferences"), JSON.stringify({
      partition: { default_zoom_level: { x: Math.log(factor) / Math.log(1.2) } },
    }));
    const context = await chromium.launchPersistentContext(profile, {
      executablePath, headless: true, viewport: null, args: ["--window-size=1440,1000"],
    });
    try {
      const page = context.pages()[0];
      await page.goto(origin);
      await keyActivate(page, page.getByRole("link", { name: "Research", exact: true }));
      await page.getByRole("heading", { name: "Research", exact: true }).waitFor();
      const layout = await page.evaluate(() => ({
        innerWidth, devicePixelRatio, scrollWidth: document.documentElement.scrollWidth,
      }));
      assert(layout.devicePixelRatio >= factor * 0.9);
      assert(layout.scrollWidth <= layout.innerWidth + 1, JSON.stringify(layout));
      await tabTo(page, page.getByLabel("Research question"));
      await page.keyboard.type("22.2");
      await keyActivate(page, page.getByRole("button", { name: "Search evidence" }));
      await keyActivate(page, page.getByRole("button", { name: "Inspect evidence" }).first());
      await keyActivate(page, page.getByRole("button", { name: /Qualifier.*Excludes EBCx/ }));
      await page.getByRole("img", { name: /Highlighted region: Excludes EBCx/ }).waitFor();
      await keyActivate(page, page.getByRole("button", { name: "Add qualified note to draft" }));
      const draft = `draft-zoom-${factor * 100}.json`;
      await download(page, "json", draft, true);
      assert(JSON.parse(fs.readFileSync(path.join(output, draft))).text.includes("Excludes EBCx"));
      await keyActivate(page, page.getByRole("link", { name: "Research", exact: true }));
      await page.locator("#research").evaluate((section) => section.scrollIntoView({ block: "start" }));
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const cdp = await context.newCDPSession(page);
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", fromSurface: true });
      const png = Buffer.from(capture.data, "base64");
      assert(png.readUInt32BE(16) >= layout.innerWidth * factor * 0.9, "Chrome capture clipped the zoomed viewport.");
      fs.writeFileSync(path.join(output, `research-zoom-${factor * 100}.png`), png);
      pass(`ZOOM_${factor * 100}`, { ...layout, keyboard_search_evidence_and_download: true });
    } finally {
      await context.close();
    }
  }
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath, headless: true });
  const version = browser.version();
  try {
    await journey(browser);
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ version, checks }, null, 2) + "\n");
  }
  fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ version, checks }, null, 2) + "\n");
  console.log(JSON.stringify({ version, checks }, null, 2));
})().catch((error) => { console.error(error); process.exitCode = 1; });
