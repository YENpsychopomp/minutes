/**
 * 截圖（開發工具）：把頁面擷取到 .ui_shots/，供 README 與驗收使用。
 * 用法：node frontend/scripts/shoot.mjs [工作編號]
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const BASE = process.env.SHOT_BASE ?? "http://127.0.0.1:8765";
const JOB = process.argv[2];
const PORT = Number(process.env.CDP_PORT ?? 9334);
const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = new URL("../../.ui_shots/", import.meta.url).pathname;
const PROFILE = new URL("../../.chrome-profile/" + PORT + "/", import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });

const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-crash-reporter",
  "--no-crash-upload", "--disable-breakpad", "--disable-dev-shm-usage",
  "--force-device-scale-factor=2", "--hide-scrollbars",
  "--user-data-dir=" + PROFILE, "--remote-debugging-port=" + PORT,
  "--remote-allow-origins=*", "about:blank",
], { stdio: "ignore" });

async function target() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const res = await fetch("http://127.0.0.1:" + PORT + "/json/list");
      const pages = (await res.json()).filter((t) => t.type === "page");
      if (pages.length) return pages[0];
    } catch { /* 還沒起來 */ }
    await sleep(250);
  }
  throw new Error("找不到 Chrome 偵錯目標");
}

class Session {
  constructor(ws) {
    this.ws = ws; this.next = 1; this.pending = new Map();
    ws.addEventListener("message", (e) => {
      const msg = JSON.parse(e.data);
      if (msg.id && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result);
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
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
}

const SHOTS = [
  { name: "1-上傳頁面", url: BASE + "/", width: 1440, height: 900, full: false },
  JOB ? { name: "2-摘要結果", url: `${BASE}/#/job/${JOB}`, width: 1440, height: 1320, full: true } : null,
  JOB ? { name: "3-逐字稿", url: `${BASE}/#/job/${JOB}`, width: 1440, height: 1000, full: false, tab: "逐字稿" } : null,
  JOB ? { name: "4-手機版", url: `${BASE}/#/job/${JOB}`, width: 390, height: 844, full: false } : null,
].filter(Boolean);

const main = async () => {
  const t = await target();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener("open", res, { once: true }); ws.addEventListener("error", rej, { once: true }); });
  const s = new Session(ws);
  await s.send("Page.enable");
  await s.send("Runtime.enable");

  for (const shot of SHOTS) {
    await s.send("Emulation.setDeviceMetricsOverride", {
      width: shot.width, height: shot.height, deviceScaleFactor: 2, mobile: shot.width < 500,
    });
    await s.send("Page.navigate", { url: shot.url });
    for (let i = 0; i < 60; i += 1) {
      if (await s.evaluate("Boolean(document.querySelector('.masthead .wordmark'))")) break;
      await sleep(200);
    }
    if (shot.tab) {
      await s.evaluate(`[...document.querySelectorAll('[role=tab]')].find((t) => t.textContent.includes(${JSON.stringify(shot.tab)}))?.click()`);
      await sleep(1200);
    } else {
      await sleep(900);
    }
    await s.send("Emulation.setScrollbarsHidden", { hidden: true }).catch(() => undefined);
    const metrics = await s.send("Page.getLayoutMetrics");
    const height = shot.full ? Math.ceil(metrics.cssContentSize.height) : shot.height;
    await s.send("Emulation.setDeviceMetricsOverride", {
      width: shot.width, height, deviceScaleFactor: 2, mobile: shot.width < 500,
    });
    await sleep(400);
    const { data } = await s.send("Page.captureScreenshot", {
      format: "png", captureBeyondViewport: Boolean(shot.full),
      clip: { x: 0, y: 0, width: shot.width, height, scale: 1 },
    });
    const file = `${OUT}${shot.name}.png`;
    writeFileSync(file, Buffer.from(data, "base64"));
    console.log(`  ✓ ${shot.name} → ${shot.width}×${height}  ${file}`);
  }

  ws.close();
  chrome.kill("SIGTERM");
};

main().catch((error) => {
  console.error("截圖失敗：", error.message);
  try { chrome.kill("SIGTERM"); } catch { /* 已結束 */ }
  process.exit(1);
});
