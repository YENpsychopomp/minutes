/**
 * UI 檢查（開發工具，不隨套件發佈）
 *
 * 用 Chrome DevTools 協議實際渲染頁面並量測：
 *   1. React 是否真的渲染出內容（不是白畫面）
 *   2. 有沒有水平捲動、元素有沒有跑出視窗
 *   3. 可點擊元素是否符合觸控尺寸
 *   4. 文字與背景的對比是否達 WCAG AA
 *   5. 順便輸出截圖到 .ui_shots/
 *
 * 用法：node frontend/scripts/ui-check.mjs [頁面URL]
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const APP = process.argv[2] && process.argv[2].startsWith("http") ? process.argv[2] : "http://127.0.0.1:8765/";
const PORT = Number(process.env.CDP_PORT || 9333);
const CHROME = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT_DIR = new URL("../../.ui_shots/", import.meta.url).pathname;
const PROFILE = new URL("../../.chrome-profile/" + PORT + "/", import.meta.url).pathname;
mkdirSync(OUT_DIR, { recursive: true });

const problems = [];
const notes = [];
const fail = (message) => { problems.push(message); console.log("  ✗ " + message); };
const pass = (message) => { notes.push(message); console.log("  ✓ " + message); };
const warn = (message) => { notes.push(message); console.log("  ! " + message); };

function luminance(rgb) {
  const f = (v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * f(rgb[0] / 255) + 0.7152 * f(rgb[1] / 255) + 0.0722 * f(rgb[2] / 255);
}
function contrast(fg, bg) {
  const parse = (value) => (value.match(/\d+(\.\d+)?/g) || []).slice(0, 3).map(Number);
  const a = luminance(parse(fg));
  const b = luminance(parse(bg));
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

const measure = (LIMIT) => `(() => {
  const rect = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
  const clickables = [...document.querySelectorAll('button, a, input[type=file], label.dropzone')]
    .filter((el) => el.offsetParent !== null)
    .map((el) => ({ tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 26), label: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 20), ...rect(el) }));
  const overflowing = [...document.querySelectorAll('body *')]
    .filter((el) => el.offsetParent !== null && el.getBoundingClientRect().right > ${LIMIT} + 1)
    .slice(0, 6)
    .map((el) => el.tagName.toLowerCase() + '.' + String(el.className).slice(0, 22));
  return {
    title: document.title,
    rendered: Boolean(document.querySelector('.masthead .wordmark')),
    hasRoot: Boolean(document.querySelector('#root') && document.querySelector('#root').children.length),
    dropzone: Boolean(document.querySelector('.dropzone')),
    stageCount: document.querySelectorAll('.stage').length,
    chapterCount: document.querySelectorAll('.chapter').length,
    lineCount: document.querySelectorAll('.line').length,
    chips: [...document.querySelectorAll('.masthead-right .num, .masthead-right .chip')].map((n) => n.textContent.trim()),
    jobCards: document.querySelectorAll('.job-card').length,
    docTitle: (document.querySelector('.doc-title') || {}).textContent || null,
    fonts: {
      body: getComputedStyle(document.body).fontFamily.split(',')[0],
      heading: document.querySelector('h2') ? getComputedStyle(document.querySelector('h2')).fontFamily.split(',')[0] : null,
      size: getComputedStyle(document.body).fontSize,
      lineHeight: getComputedStyle(document.body).lineHeight
    },
    pageWidth: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
    limit: ${LIMIT},
    clickables: clickables,
    overflowing: overflowing
  };
})()`;

const COLORS = `(() => {
  const bgOf = (el) => { let node = el; while (node) { const c = getComputedStyle(node).backgroundColor; if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c; node = node.parentElement; } return 'rgb(251, 246, 236)'; };
  const out = [];
  for (const sel of ['.drop-title', '.drop-hint', '.stage-title', '.stage-hint', '.line-text', '.chapter-head h4', '.pull', '.tag', '.kv dd', '.btn-primary', '.time-chip', '.footer', '.job-card-meta']) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const cs = getComputedStyle(el);
    out.push({ sel: sel, fg: cs.color, bg: bgOf(el), size: cs.fontSize });
  }
  return out;
})()`;


const INTERACT = `(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { clicked: [], errors: [] };
  try {
    const tabs = [...document.querySelectorAll('[role=tab]')];
    const transcriptTab = tabs.find((t) => t.textContent.includes('逐字稿'));
    if (transcriptTab) { transcriptTab.click(); out.clicked.push('逐字稿'); }
    await wait(900);
    out.lines = document.querySelectorAll('.line').length;
    out.player = Boolean(document.querySelector('.player audio'));
    const timeButtons = [...document.querySelectorAll('.line-time button')];
    const pick = timeButtons.find((b) => b.textContent.trim() !== '0:00') || timeButtons[0];
    out.pickedClock = pick ? pick.textContent.trim() : null;
    if (pick) { pick.click(); await wait(500); }
    const audio = document.querySelector('.player audio');
    out.audioSrc = audio ? (audio.currentSrc || audio.src) : null;
    out.currentTime = audio ? Number(audio.currentTime.toFixed(2)) : null;
    out.duration = audio && Number.isFinite(audio.duration) ? Number(audio.duration.toFixed(1)) : null;
    const input = document.getElementById('find');
    if (input) {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, '冷氣');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await wait(400);
      out.linesFiltered = document.querySelectorAll('.line').length;
      out.filterChip = (document.querySelector('.transcript-toolbar .chip.num') || {}).textContent || null;
      setter.call(input, '');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await wait(300);
    }
    const summaryTab = tabs.find((t) => t.textContent.includes('摘要'));
    if (summaryTab) { summaryTab.click(); await wait(400); out.clicked.push('摘要'); }
    out.chips = document.querySelectorAll('.time-chip').length;
    out.downloads = document.querySelectorAll('.download-row a').length;
    const rawTab = tabs.find((t) => t.textContent.includes('原始'));
    if (rawTab) { rawTab.click(); await wait(900); out.rawLength = ((document.querySelector('.raw-json') || {}).textContent || '').length; }
  } catch (error) { out.errors.push(String(error && error.message || error)); }
  return out;
})()`;

const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-crash-reporter",
  "--no-crash-upload", "--disable-breakpad", "--disable-dev-shm-usage",
  "--user-data-dir=" + PROFILE, "--remote-debugging-port=" + PORT,
  "--remote-allow-origins=*", "about:blank",
], { stdio: "ignore" });

async function findTarget() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const res = await fetch("http://127.0.0.1:" + PORT + "/json/list");
      const list = (await res.json()).filter((item) => item.type === "page");
      if (list.length) return list[0];
    } catch { /* 瀏覽器還沒起來 */ }
    await sleep(250);
  }
  throw new Error("拿不到 Chrome 的偵錯目標");
}

class Session {
  constructor(ws) {
    this.ws = ws; this.next = 1; this.pending = new Map();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const entry = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) entry.reject(new Error(JSON.stringify(msg.error)));
        else entry.resolve(msg.result);
      }
    });
  }
  send(method, params = {}) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception && result.exceptionDetails.exception.description || "頁內腳本錯誤");
    return result.result.value;
  }
}

async function capture(session, name) {
  const { data } = await session.send("Page.captureScreenshot", { format: "png" });
  const file = OUT_DIR + name + ".png";
  writeFileSync(file, Buffer.from(data, "base64"));
  pass("截圖 → " + file);
}

async function main() {
  const target = await findTarget();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const session = new Session(ws);
  await session.send("Page.enable");
  await session.send("Runtime.enable");

  for (const size of [
    { name: "desktop", width: 1440, height: 980 },
    { name: "tablet", width: 834, height: 1000 },
    { name: "mobile", width: 375, height: 812 },
  ]) {
    console.log("\n[" + size.name + "] " + size.width + "×" + size.height + "  " + APP);
    await session.send("Emulation.setDeviceMetricsOverride", { width: size.width, height: size.height, deviceScaleFactor: 1, mobile: size.name === "mobile" });
    await session.send("Page.navigate", { url: APP });
    for (let i = 0; i < 40; i += 1) {
      if (await session.evaluate("Boolean(document.querySelector('.masthead .wordmark'))")) break;
      await sleep(250);
    }
    const data = await session.evaluate(measure(size.width));
    if (!data.rendered) fail("React 沒有渲染出內容（白畫面？）");
    else pass("已渲染：" + data.title);
    if (!data.hasRoot) fail("#root 是空的");
    if (data.dropzone) pass("上傳區存在");
    if (data.overflowing.length) fail("元素超出視窗：" + data.overflowing.join("、"));
    else pass("無元素超出 " + data.limit + "px 寬度");
    if (data.pageWidth > size.width + 1) fail("出現水平捲動：" + data.pageWidth + " > " + size.width);
    if (data.chips.length) pass("列頭顯示：" + data.chips.join(" / "));
    pass("字體：內文 " + data.fonts.body + " " + data.fonts.size + "／行高 " + data.fonts.lineHeight + "；標題 " + data.fonts.heading);
    if (data.docTitle) pass("摘要標題：" + data.docTitle.trim().slice(0, 30));
    if (data.chapterCount) pass("章節 " + data.chapterCount + " 個、逐字稿 " + data.lineCount + " 行");
    if (data.jobCards) pass("工作紀錄 " + data.jobCards + " 筆");
    if (data.stageCount) pass("進度階段 " + data.stageCount + " 段");

    const small = data.clickables.filter((item) => item.tag === "button" && item.h > 0 && item.h < 34 && !/ghost|time-chip|tab/.test(item.cls));
    if (small.length) fail("按鍵高度不足 34px：" + small.map((item) => (item.cls || item.label || item.tag) + "=" + item.h + "px").join("、"));
    else pass("可點擊元素 " + data.clickables.length + " 個，主要按鈕尺寸合格");

    if (size.name === "desktop") await capture(session, "desktop");
  }

  await session.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 980, deviceScaleFactor: 1, mobile: false });
  await session.send("Page.navigate", { url: APP });
  await sleep(1200);

  console.log("\n[互動] 分頁、搜尋、時間戳跳播放");
  const act = await session.evaluate(INTERACT);
  if (act.errors.length) fail("頁內錯誤：" + act.errors.join("；"));
  if (act.lines) pass("逐字稿分頁渲染 " + act.lines + " 行");
  else fail("逐字稿分頁沒有渲染任何行");
  if (act.player) pass("播放器已掛上 <audio>");
  else warn("沒有播放器（此工作可能沒有可播放的原始檔）");
  const toSeconds = (clock) => {
    if (!clock) return null;
    const parts = clock.split(":").map(Number);
    return parts.length === 3 ? parts[0] * 3600 + parts[1] * 60 + parts[2] : parts[0] * 60 + parts[1];
  };
  const expected = toSeconds(act.pickedClock);
  if (typeof act.currentTime === "number" && expected !== null && Math.abs(act.currentTime - expected) <= 1.5) {
    pass("點時間戳 " + act.pickedClock + " → 實際播放位置 " + act.currentTime + "s（Range 串流可用）");
  } else if (act.player) {
    fail("點時間戳 " + act.pickedClock + " 後 currentTime = " + act.currentTime + "（media 端點或 seek 接錯）");
  }
  if (act.duration) pass("音訊長度 " + act.duration + "s");
  if (typeof act.linesFiltered === "number" && act.linesFiltered < act.lines) {
    pass("搜尋可用：" + (act.filterChip || "").trim() + "（" + act.lines + " → " + act.linesFiltered + "）");
  } else if (typeof act.linesFiltered === "number") {
    fail("搜尋沒有過濾結果（" + act.linesFiltered + "）");
  }
  if (act.chips) pass("摘要裡 " + act.chips + " 個可點擊時間戳");
  if (act.downloads) pass("下載連結 " + act.downloads + " 條");
  if (act.rawLength > 200) pass("原始 JSON 分頁載入 " + act.rawLength + " 字元");
  await capture(session, "transcript");

  console.log("\n[對比] WCAG AA：內文 4.5:1、22px 以上 3:1");
  for (const item of await session.evaluate(COLORS)) {
    const value = Number(contrast(item.fg, item.bg).toFixed(2));
    const big = Number.parseFloat(item.size) >= 22;
    if (value >= (big ? 3 : 4.5)) pass(item.sel + " " + value + ":1");
    else fail(item.sel + " 只有 " + value + ":1（字級 " + item.size + "）");
  }

  ws.close();
  chrome.kill("SIGTERM");
  console.log("\n總結：" + notes.length + " 項通過，" + problems.length + " 項待修");
  if (problems.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error("檢查失敗：", error.message);
  try { chrome.kill("SIGTERM"); } catch { /* 已結束 */ }
  process.exit(1);
});
