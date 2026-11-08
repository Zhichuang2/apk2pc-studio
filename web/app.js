/* APK2PC Studio · 前端逻辑（零依赖） */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

/* 前端一旦报错就地显示，不要把界面变成「一片静止」 */
window.addEventListener("error", (ev) => {
  try {
    document.title = "ERR: " + ev.message;
    let box = document.getElementById("jserr");
    if (!box) {
      box = document.createElement("pre");
      box.id = "jserr";
      box.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:999;margin:0;padding:8px 12px;" +
        "background:#3a1418;color:#ffc9c9;font:11px/1.5 monospace;border-top:1px solid #6a2c31;max-height:40vh;overflow:auto";
      document.body.appendChild(box);
    }
    box.textContent += `${ev.message}  @${ev.filename}:${ev.lineno}\n`;
  } catch (_) { /* 忽略 */ }
});

const S = {
  env: null,
  apk: "",
  info: {},
  jobId: null,
  cursor: 0,
  timer: null,
  result: {},
  running: false,
  launchDone: false,
};

/* ------------------------------------------------ HTTP */
async function api(path, body, timeoutMs = 20000) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  // 兜底超时：请求挂死时不能让界面永远停在「准备中…」
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  opt.signal = ctrl.signal;
  try {
    const r = await fetch(path, opt);
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("json")) return {};
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------ 环境 */
async function refreshState() {
  const s = await api("/api/state");
  if (s && s.app) renderEnv(s);
}

function renderEnv(s) {
  S.env = s;
  $("#ver").textContent = `v${s.app.version}${s.app.frozen ? " · exe" : ""}`;
  $("#data-dir").textContent = "数据目录 " + s.data_dir;
  const od = $("#opt-output-dir");
  if (od && document.activeElement !== od) {
    od.value = (s.settings && s.settings.output_dir) || "";
  }
  const oh = $("#output-hint");
  if (oh) oh.textContent = "当前：" + (s.output_dir || "—");
  const bl = $("#opt-beta-light");
  if (bl && document.activeElement !== bl) {
    bl.checked = !!(s.settings && s.settings.beta_light_runtime);
  }

  const tools = s.tools || [];
  const okCount = tools.filter((t) => t.ok).length;
  $("#ready-tag").textContent = `${okCount}/${tools.length} 就绪`;

  const box = $("#tools");
  box.innerHTML = "";
  tools.forEach((t) => {
    const el = document.createElement("div");
    el.className = "tool " + (t.ok ? "ok" : "bad");
    el.innerHTML = `
      <span class="st">${t.ok ? "●" : "○"}</span>
      <span class="nm">${t.name}</span>
      ${t.ok ? `<span class="path" title="${esc(t.path)}">${esc(shortPath(t.path))}</span>` :
        (t.installable ? `<button class="btn ghost xs" data-install="${t.key}">装配</button>` : `<span class="tag">缺失</span>`)}
      <div class="why">${esc(t.hint)}</div>`;
    box.appendChild(el);
  });
  box.querySelectorAll("[data-install]").forEach((b) => {
    b.onclick = () => {
      const target = b.dataset.install;
      const size = target === "emulator" || target === "sysimg" ? "（约 1.5–2 GB，需耐心）" : "";
      if (confirm(`开始下载并装配：${b.closest(".tool").querySelector(".nm").textContent} ${size}？`)) {
        startJob("provision", { target: target === "sysimg" ? "emulator" : target });
      }
    };
  });

  // 顶部提示条
  const banner = $("#banner");
  const msgs = [];
  if (s.blocking && s.blocking.length) {
    msgs.push(`<span>缺少必需组件：${esc(s.blocking.join("、"))}，先装 JDK 11+ 再回来</span>`);
  }
  if (!s.can_java) msgs.push(`<span>jadx 未就绪 —— Java 源码这一步会被跳过</span><button class="btn sm" id="b-jadx">装配 jadx</button>`);
  if (!s.can_build) msgs.push(`<span>签名工具缺失 —— 重建后无法安装</span><button class="btn sm" id="b-bt">装配签名工具</button>`);
  if (!s.can_run) msgs.push(`<span>运行时缺失 —— 无法在电脑上跑 APK</span><button class="btn sm" id="b-emu">装配模拟器</button>`);
  if (msgs.length) {
    banner.className = "banner" + (s.blocking && s.blocking.length ? " err" : "");
    banner.innerHTML = msgs.join("");
    const bj = $("#b-jadx"); if (bj) bj.onclick = () => startJob("provision", { target: "jadx" });
    const bb = $("#b-bt"); if (bb) bb.onclick = () => startJob("provision", { target: "buildtools" });
    const be = $("#b-emu"); if (be) be.onclick = () => startJob("provision", { target: "emulator" });
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  // 恢复上次 APK
  if (!S.apk && s.settings && s.settings.last_apk) {
    S.apk = s.settings.last_apk;
    $("#apk-path").value = S.apk;
    scanApk(true);
  }
  // 启动器模式：自动跑
  if (s.launch && !S.launchDone) {
    S.launchDone = true;
    attachJob(s.launch.job, s.launch.title);
  }
}

function shortPath(p) {
  if (!p) return "";
  const parts = p.split(/[\\/]/);
  return parts.length > 2 ? "…\\" + parts.slice(-2).join("\\") : p;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ------------------------------------------------ 设备 */
function renderDevices(list) {
  const box = $("#devices");
  if (!list || !list.length) {
    box.innerHTML = `<p class="muted sm">没有在线设备。点「启动模拟器」或先装运行时。</p>`;
    updateShot();
    return;
  }
  box.innerHTML = "";
  list.forEach((d) => {
    const el = document.createElement("div");
    el.className = "dev";
    const on = d.state === "device";
    el.innerHTML = `<span class="dot ${on ? "" : "off"}"></span>
      <span class="id">${esc(d.serial)}</span>
      <span class="muted xs">${esc(d.model || d.kind)}</span>`;
    box.appendChild(el);
  });
  updateShot();
}

async function refreshDevices() {
  const r = await api("/api/devices");
  renderDevices(r.devices || []);
}

let shotTimer = null;
function updateShot() {
  const has = (S.env && S.env.devices || []).some((d) => d.state === "device");
  const box = $("#shot");
  if (!has) {
    box.innerHTML = `<span class="muted xs">无画面（设备未就绪）</span>`;
    if (shotTimer) { clearInterval(shotTimer); shotTimer = null; }
    return;
  }
  if (!shotTimer) {
    shotTimer = setInterval(() => shotOnce(true), 4000);
    shotOnce();
  }
}

function shotOnce(silent) {
  const box = $("#shot");
  const serial = (S.env && (S.env.devices || []).find((d) => d.state === "device") || {}).serial || "";
  const img = new Image();
  img.onload = () => { box.innerHTML = ""; box.appendChild(img); };
  img.onerror = () => { if (!silent) box.innerHTML = `<span class="muted xs">截屏失败</span>`; };
  img.src = `/api/screencap?serial=${encodeURIComponent(serial)}&t=${Date.now()}`;
}

/* ------------------------------------------------ APK */
async function pickApk() {
  const r = await api("/api/pick", { kind: "file" });
  if (r.path) {
    $("#apk-path").value = r.path;
    S.apk = r.path;
    scanApk();
  }
}

async function scanApk(silent) {
  const p = $("#apk-path").value.trim();
  if (!p) return;
  S.apk = p;
  const r = await api("/api/scan", { apk: p });
  const box = $("#apk-info");
  if (r.error) {
    if (!silent) toast(`无法读取：${r.error}`, "err");
    box.classList.add("hidden");
    return;
  }
  const peek = r.peek || {};
  const bad = r.badging || {};
  $("#apk-size").textContent = r.size;
  box.classList.remove("hidden");
  box.innerHTML = `
    <dt>文件名</dt><dd>${esc(p.split(/[\\/]/).pop())}</dd>
    <dt>包名</dt><dd>${esc(bad.package || "需解包后识别")}</dd>
    <dt>DEX</dt><dd>${peek.dex || 0} 个</dd>
    <dt>ABI</dt><dd>${(peek.libs || []).join(" ") || "无原生库"}</dd>`;
  if (peek.packed_hint) {
    const d = document.createElement("div");
    d.className = "xs";
    d.style.cssText = "grid-column:1/-1;color:var(--warn);margin-top:6px";
    d.textContent = peek.packed_hint;
    box.appendChild(d);
  }
}

/* ------------------------------------------------ 任务 */
const STEP_TPL = {
  full: ["准备工作区", "apktool 解包（资源 + smali）", "反编译为 Java 源码",
         "apktool 重建 APK", "zipalign + 签名", "安装并运行在电脑上"],
  decode: ["准备工作区", "apktool 解包（资源 + smali）", "反编译为 Java 源码"],
  java: ["准备工作区", "反编译为 Java 源码"],
  build: ["apktool 重建 APK", "zipalign + 签名"],
  run: ["安装并运行在电脑上"],
  light: ["静态扫描 APK 依赖", "生成 BETA 评估报告"],
  provision: ["下载并装配组件"],
};

/* 点下去立刻有反馈：先把步骤骨架铺出来，不等后端第一个回包 */
function showSkeleton(action) {
  const names = STEP_TPL[action] || STEP_TPL.full;
  $("#steps").innerHTML = names.map((n) =>
    `<li class="step" data-key=""><span class="ic"></span><span class="tx">${esc(n)}</span><span class="tick"></span></li>`
  ).join("");
  $("#bar").style.width = "0%";
  $("#job-state").textContent = "提交中…";
  $("#job-state").style.color = "";
  $("#log").innerHTML = "";
  setButtonsEnabled(false);
  $("#btn-cancel").classList.remove("hidden");
  const btn = $("#btn-full");
  btn.dataset.old = btn.textContent;
  btn.textContent = "准备中…";
}

function restoreUi() {
  setButtonsEnabled(true);
  $("#btn-cancel").classList.add("hidden");
  $("#job-state").textContent = "空闲";
  $("#job-state").style.color = "";
  const btn = $("#btn-full");
  if (btn.dataset.old) { btn.textContent = btn.dataset.old; delete btn.dataset.old; }
}

async function startJob(action, extra) {
  if (action !== "provision" && !S.apk) {
    toast("先选一个 APK 文件", "err");
    return;
  }
  showSkeleton(action);
  const payload = Object.assign({
    action,
    apk: S.apk,
    fullscreen: $("#opt-fullscreen").checked,
    serial: null,
    decoded: (S.result && S.result.decoded) || null,
  }, extra || {});

  let r;
  try {
    r = await api("/api/job", payload);
  } catch (e) {
    restoreUi();
    toast("提交失败：" + (e.message || e), "err");
    return;
  }
  if (r.job) attachJob(r.job, r.title);
  else { restoreUi(); toast(r.error || "提交失败", "err"); }
}

function attachJob(id, title) {
  S.jobId = id;
  S.cursor = 0;
  S.running = true;
  S.t0 = Date.now();
  S.jobTitle = title || "运行中";
  $("#job-state").textContent = S.jobTitle;
  $("#btn-cancel").classList.remove("hidden");
  setButtonsEnabled(false);
  clearInterval(S.timer);
  clearInterval(S.tickTimer);
  S.timer = setInterval(pollJob, 300);
  S.tickTimer = setInterval(tickRunning, 1000);
  pollJob();
}

/* 让「等待」看得见：跑着的步骤显示秒表；装应用那步没设备时提示在起模拟器 */
function tickRunning() {
  const el = $("#steps .step.running");
  if (!el || !S.running) return;
  const sec = Math.round((Date.now() - (S.t0 || Date.now())) / 1000);
  const tick = el.querySelector(".tick");
  const noDev = !((S.env && S.env.devices) || []).some((d) => d.state === "device");
  const isRun = el.dataset.key === "run";
  if (tick) tick.textContent = (isRun && noDev) ? `起模拟器 ${sec}s` : `${sec}s`;
  $("#job-state").textContent = `${S.jobTitle || "运行中"} · ${sec}s`;
}

async function pollJob() {
  if (!S.jobId) return;
  const j = await api(`/api/job?id=${S.jobId}&since=${S.cursor}`);
  // 注意：任务对象里始终带 error 字段（失败时是文案，成功时是空串），
  // 所以只能按 status 是否存在判断「任务查不到」，否则失败任务会把轮询掐死。
  if (!j.status) {
    clearInterval(S.timer);
    clearInterval(S.tickTimer);
    S.timer = null;
    S.running = false;
    restoreUi();
    toast("任务状态丢失：" + (j.error || "未知"), "err");
    return;
  }
  S.cursor = j.cursor;
  (j.logs || []).forEach(appendLog);
  renderSteps(j.steps, j.progress);
  S.result = j.result || {};

  if (j.status !== "running") {
    clearInterval(S.timer);
    clearInterval(S.tickTimer);
    S.timer = null;
    S.running = false;
    $("#btn-cancel").classList.add("hidden");
    setButtonsEnabled(true);
    const ok = j.status === "ok";
    $("#job-state").textContent =
      ok ? `完成 · ${j.elapsed}s` : j.status === "cancelled" ? "已取消" : "失败";
    $("#job-state").style.color = ok ? "var(--acc)" : j.status === "error" ? "var(--err)" : "";
    const btn = $("#btn-full");
    if (btn.dataset.old) { btn.textContent = btn.dataset.old; delete btn.dataset.old; }
    if (j.status === "error" && j.error) appendLog({ level: "error", text: "✘ " + j.error, t: j.elapsed });
    refreshState();
    refreshDevices();
    renderArtifacts();
    if (ok) toast(j.kind === "full" ? "全流程完成，应用已在电脑上运行" : "完成", "");
    else if (j.status === "error") toast(j.error || "任务失败，日志里有原因", "err");
  }
}

function setButtonsEnabled(on) {
  $$("[data-act]").forEach((b) => (b.disabled = !on));
  $("#btn-full").disabled = !on;
}

function renderSteps(steps, progress) {
  const box = $("#steps");
  const running = (steps || []).find((s) => s.status === "running");
  box.innerHTML = (steps || []).map((s) => {
    const cls = s.status === "pending" ? "" : s.status;
    return `<li class="step ${cls}" data-key="${esc(s.key || "")}" title="${esc(s.detail || s.name)}">
      <span class="ic"></span><span class="tx">${esc(s.name)}</span><span class="tick"></span></li>`;
  }).join("");
  $("#bar").style.width = (progress ? progress.pct : 0) + "%";
  // 步骤整体重绘后立刻补一次秒表，避免「重绘→归零→1 秒后才有字」的跳动
  if (running) tickRunning();
}

function appendLog(l) {
  const box = $("#log");
  const span = document.createElement("span");
  span.className = "l " + (l.level || "out");
  span.innerHTML = `<span class="t">${l.t != null ? String(l.t).padStart(7, " ") + "s" : ""}</span>${esc(l.text)}`;
  box.appendChild(span);
  if ($("#opt-autoscroll").checked) box.scrollTop = box.scrollHeight;
}

function renderArtifacts() {
  const r = S.result || {};
  const bits = [];
  if (r.decoded) bits.push("解包目录 ✓");
  if (r.java) bits.push("Java ✓");
  if (r.signed) bits.push("签名 APK ✓");
  if (r.serial) bits.push("已运行 @ " + r.serial);
  if (r.report) bits.push("BETA 报告 ✓");
  const line = $("#artifact-line");
  line.textContent = bits.length ? bits.join(" · ") : "尚未生成产物";
  line.style.color = bits.length ? "var(--acc)" : "";
}

/* ------------------------------------------------ 小提示 */
let flashTimer = null;
function flash(text, isErr) {
  const b = $("#banner");
  b.className = "banner" + (isErr ? " err" : "");
  b.textContent = text;
  b.classList.remove("hidden");
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    if (S.env) renderEnv(S.env);
  }, 4000);
}

/* 轻量反馈：右下角飘一条，不抢顶部告警区 */
function toast(msg, kind) {
  const box = $("#toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 260);
  }, 2600);
}

/* ------------------------------------------------ 绑定 */
function bind() {
  $("#btn-pick").onclick = (e) => { e.stopPropagation(); pickApk(); };
  // 整块拖放区可点，但不要和里面的输入框/按钮抢事件
  $("#dropzone").onclick = (e) => {
    if (e.target.closest("button, input")) return;
    pickApk();
  };
  $("#btn-scan").onclick = () => scanApk();
  $("#apk-path").addEventListener("keydown", (e) => { if (e.key === "Enter") scanApk(); });

  $("#btn-full").onclick = () => {
    if (S.running) return;
    if (S.apk) startJob("full");
    else toast("先选择 APK", "err");
  };
  $$("[data-act]").forEach((b) => {
    b.onclick = () => { if (!S.running) startJob(b.dataset.act); };
  });
  $("#btn-cancel").onclick = async () => {
    if (S.jobId) { toast("正在取消…", "warn"); await api("/api/cancel", { id: S.jobId }); }
  };

  // 快捷键：Esc 取消，Ctrl/Cmd+Enter 跑全流程
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.running) $("#btn-cancel").click();
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !S.running) $("#btn-full").click();
  });

  $("#btn-dev-refresh").onclick = refreshDevices;
  $("#btn-dev-start").onclick = async () => {
    const btn = $("#btn-dev-start");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "启动中…";
    try {
      const r = await api("/api/emulator", { action: "start", fullscreen: $("#opt-fullscreen").checked });
      toast(r.ok ? "模拟器已拉起，等它开机完成" : "启动失败，日志里有原因", r.ok ? "" : "err");
    } finally {
      btn.disabled = false;
      btn.textContent = old;
      refreshDevices();
    }
  };

  $("#btn-output-save").onclick = async () => {
    const btn = $("#btn-output-save");
    const val = ($("#opt-output-dir").value || "").trim().replace(/^"|"$/g, "");
    btn.disabled = true;
    try {
      const r = await api("/api/settings", { output_dir: val });
      if (r && r.error) toast("保存失败：" + r.error, "err");
      else toast(val ? "成品目录已切换" : "已恢复默认目录");
      await refreshState();
    } finally {
      btn.disabled = false;
    }
  };

  $("#btn-workspace").onclick = () => {
    if (S.env) fetch(`/api/open?path=${encodeURIComponent(S.env.workspace)}`);
  };
  $("#btn-open-out").onclick = () => {
    const p = (S.result && (S.result.out_dir || S.result.workspace)) || (S.env && S.env.workspace);
    if (p) fetch(`/api/open?path=${encodeURIComponent(p)}`);
  };
  $("#btn-open-report").onclick = () => {
    const p = S.result && S.result.report;
    if (p) fetch(`/api/open?path=${encodeURIComponent(p)}`);
    else toast("先跑一次「BETA 轻量评估」", "warn");
  };
  $("#opt-beta-light").onchange = async () => {
    const v = $("#opt-beta-light").checked;
    const r = await api("/api/settings", { beta_light_runtime: v });
    if (r && r.error) toast("保存失败：" + r.error, "err");
    else toast(v ? "BETA 已开启：全流程会附带静态评估" : "BETA 已关闭", "");
  };
  $("#btn-quit").onclick = async () => {
    if (confirm("退出 APK2PC Studio？")) { await api("/api/shutdown", {}); window.close(); }
  };

  $("#btn-export-gradle").onclick = async (e) => {
    const btn = e.currentTarget;
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = "生成中…";
    try {
      const r = await api("/api/export", { kind: "gradle", job: S.jobId });
      if (r.error) toast("导出失败：" + r.error, "err");
      else toast("工程已生成：" + r.path, "");
    } finally { btn.disabled = false; btn.textContent = old; }
  };
  $("#btn-export-launcher").onclick = async (e) => {
    const btn = e.currentTarget;
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = "打包中…（大包要几十秒）";
    try {
      const r = await api("/api/export", { kind: "launcher", job: S.jobId });
      if (r.error) toast("生成失败：" + r.error, "err");
      else toast("启动器已生成：" + r.path, "");
    } finally { btn.disabled = false; btn.textContent = old; }
  };

  $("#btn-shot").onclick = () => shotOnce();
  $("#btn-clear-log").onclick = () => ($("#log").innerHTML = "");
  $("#btn-copy-log").onclick = async () => {
    try { await navigator.clipboard.writeText($("#log").innerText); toast("日志已复制", ""); }
    catch { toast("复制失败", "err"); }
  };
}

/* ------------------------------------------------ 启动 */
(async function boot() {
  bind();
  await refreshState();
  await refreshDevices();
  setInterval(refreshDevices, 6000);
  // 页面加载时若已有任务在跑，接上去
  if (S.env && S.env.jobs && S.env.jobs.length) {
    const j = S.env.jobs[0];
    if (j.status === "running" && !S.jobId) attachJob(j.id, j.title);
  }
})();
