// Walks the New audience journey in a real browser and checks the gating at each step (24 checks).
// Optional, not part of `make test` because it needs a browser:
//   npm install --no-save playwright-core
//   PORT=8010 make run            # in another terminal; works offline (it only uses the example requests)
//   BASE=http://127.0.0.1:8010 CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" node scripts/ui_walkthrough.mjs
// It approves one audience and simulates its send, so run it against a test instance, not a shared one.
import { chromium } from "playwright-core";
const BASE = process.env.BASE || "http://127.0.0.1:8010";
const OUT = process.env.OUT;  // set to a folder to also save screenshots
const browser = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
let dialogAnswer = "Luis (Analytics)";
page.on("dialog", (d) => d.accept(dialogAnswer));
const checks = {};
const txt = (sel) => page.$eval(sel, (e) => e.textContent.replace(/\s+/g, " ").trim()).catch(() => "");
const visible = (sel) => page.$eval(sel, (e) => !!(e.offsetParent || e.getClientRects().length)).catch(() => false);
const disabled = (sel) => page.$eval(sel, (b) => b.disabled);
const shot = (name) => (OUT ? page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true }) : null);
async function interpret(text) {
  await page.fill("#request", text);
  await page.click("#interpret-btn");
  await page.waitForFunction(() => document.querySelector("#interpret-btn").textContent === "Interpret", { timeout: 60000 });
  await page.waitForTimeout(900);
}

await page.goto(BASE, { waitUntil: "networkidle" });
await page.waitForTimeout(500);
checks.landing_only_step1 = (await visible("#ask")) && !(await visible("#result")) && !(await visible("#history"));
await shot("1-describe");

await interpret("Active customers on Android with more than 10 GB of data in the last 30 days");
checks.moved_to_review = (await visible("#result")) && !(await visible("#ask"));
checks.continue_locked_until_answered = (await disabled("#to-size")) && /Answer the question/.test(await txt("#review-hint"));
await shot("2a-review-question");
await (await page.$("#clarify button")).click();
await page.waitForTimeout(1600);
checks.continue_unlocked = !(await disabled("#to-size"));
await shot("2b-review-ready");

await page.click("#to-size");
await page.waitForTimeout(500);
checks.size_step = await visible("#size-card");
await shot("3-size");

await page.click("#to-plan");
await page.waitForTimeout(500);
checks.plan_step = await visible("#approve-card");
checks.approve_hint_lists_missing = /Add what the campaign is for, a campaign type \(or Other\), an audience name, your name/.test(await txt("#approve-hint"));
await page.fill("#goal", "Upsell a bigger data bundle");
await page.selectOption("#campaign-type", "data_upsell");
await page.waitForTimeout(1600);
await page.click("#advise-btn");
await page.waitForFunction(() => document.querySelector("#advise-btn").textContent === "Ask the advisor", { timeout: 60000 });
await page.waitForTimeout(500);
checks.advice_shown = await visible("#advice");  // offline it says the advisor is unavailable, which is the designed behaviour
await shot("4a-plan-advice");
await page.fill("#aud-name", "Heavy-data Android upsell");
await page.fill("#approver", "Ana (Marketing Ops)");
await page.waitForTimeout(300);
checks.approve_enabled = !(await disabled("#approve-btn"));
await page.click("#approve-btn");
await page.waitForFunction(() => !document.querySelector("#done-card").classList.contains("hidden"), { timeout: 20000 });
await page.waitForTimeout(500);
checks.done_screen = /approved/.test(await txt("#done-title")) && !(await visible("#stepper"));
await shot("5-approved");

await page.click("#go-audiences");
await page.waitForTimeout(1200);
checks.audiences_view = (await visible("#history")) && !(await visible("#view-build"));
checks.tab_count = (await txt("#count-audiences")) === "1";
await page.click(".simulate-btn");
await page.waitForTimeout(2500);
checks.pending_card = /Not learned from until someone checks it/.test(await txt("#audit"));
await page.click(".confirm-btn");
await page.waitForTimeout(2000);
checks.confirmed = /confirmed by Luis/.test(await txt("#audit"));
await shot("6-audiences-results");

await page.click('.tab[data-view="insights"]');
await page.waitForTimeout(1000);
checks.insights_view = await visible("#insights-card");
checks.insights_count_choice = /Used the network/.test(await txt("#ins-terms"));
await shot("7-insights");

await page.click('.tab[data-view="build"]');
await page.waitForTimeout(400);
checks.build_keeps_done_screen = await visible("#done-card");
await page.click("#new-audience").catch(() => {});
await page.waitForTimeout(400);
checks.new_audience_resets = (await visible("#ask")) && (await page.$eval("#request", (t) => t.value)) === "";

await interpret("Postpaid customers with a high credit risk band");
checks.policy_blocks_continue = (await disabled("#to-size")) && /Fix the highlighted/.test(await txt("#review-hint"));
await shot("8-review-blocked-policy");

await page.click(".step-nav .back[data-go='1']");
await page.waitForTimeout(300);
checks.back_to_describe = await visible("#ask");
await interpret("New customers on premium plans who called the call centre twice");
checks.unmapped_blocks = (await disabled("#to-size")) && /Tick/.test(await txt("#review-hint"));
await page.check("#ack");
await page.waitForTimeout(1600);
checks.ack_unlocks = !(await disabled("#to-size"));
await shot("9-review-unmapped");
await page.click("#to-size");
await page.waitForTimeout(300);
await page.click('#stepper li[data-step="2"]');
await page.waitForTimeout(300);
checks.stepper_click_back = await visible("#result");
await page.click('#stepper li[data-step="4"]');
await page.waitForTimeout(300);
checks.stepper_jump_forward_when_unlocked = await visible("#approve-card");

console.log(JSON.stringify(checks, null, 1));
console.log("errors:", errors);
console.log(`${Object.values(checks).filter(Boolean).length} of ${Object.keys(checks).length} checks passed`);
await browser.close();
