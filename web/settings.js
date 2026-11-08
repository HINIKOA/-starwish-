// 星愿记忆 —— 设置（v0.11.0 重做）：整页设置视图（左上「← 返回」），仿 Cherry Studio：
// 左栏分组导航（基础 / 工具 / 偏好，可搜索）→ 有子页的分区多一栏子导航 → 右侧圆角卡片（标题、细分隔线、左标签右控件、灰色说明）。
// 所有开关、输入改了就自动保存。浅色 / 深色都好看，跟随主题颜色。
let C = null;            // 由 app.js 传入：{ api, toast, esc, safe, refresh, embedPanel }
let cfg = null;          // /api/settings（掩码后的配置）
let presets = [];
let page = "providers";
let sub = {};            // 各分区当前子页
let selId = "";
let provFilter = "";
let modelFilter = "";
let saveTimer = null;
let navFilter = "";

const GROUPS = [
  ["基础", [["profile", "☺", "个人信息"], ["providers", "☁", "模型服务"], ["defaults", "◎", "默认模型"], ["embed", "✶", "本地模型"]]],
  ["工具", [["mcp", "⇄", "MCP 与代理"], ["skills", "✧", "技能"], ["watch", "◉", "对话监控"], ["eye", "◈", "宇宙之眼"], ["voice", "♪", "宇宙之声"]]],
  ["偏好", [["general", "⚙", "通用"], ["appearance", "◐", "外观"], ["data", "▤", "数据"], ["core", "✦", "核心记忆"], ["about", "ⓘ", "关于"]]],
];
const SUBS = {
  data: [["", [["dir", "▤", "数据目录"]]], ["云备份设置", [["local", "▣", "本地备份"], ["webdav", "☁", "WebDAV"], ["cloud", "⇅", "云服务器"],
    ["jianguo", "◌", "坚果云配置", true], ["s3", "▭", "S3 兼容存储", true]]], ["导入设置", [["import", "⇩", "导入外部应用数据"]]]],
};
const KIND = { chat: "对话", vision: "识图", embedding: "向量", other: "其他" };
const ROLES = [
  ["chat", "对话模型（主模型）", "对话抽屉、整理星系用它；代理转发给聊天客户端时默认也用它", ["chat", "vision"]],
  ["maintain", "整理模型", "对话监控、后台整理、融合归纳、技能巡检用；留空 = 和对话模型相同。建议选便宜快速的", ["chat", "vision"]],
  ["vision", "识图模型", "宇宙之眼「通用帧模式」看屏幕 / 摄像头时用，必须支持图片输入", ["vision", "chat"]],
  ["embed", "向量模型（接口档）", "向量模型选「③ 接口」时用；内置 bge-small 不需要它", ["embedding"]],
];

const $ = (s) => document.querySelector(s);
const E = (s) => C.esc(s);
const uid = (p) => `${p}-${Math.random().toString(36).slice(2, 6)}`;
const fmtT = (t) => (t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—");
const fmtSize = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n > 1024 ? (n / 1024).toFixed(0) + " KB" : (n || 0) + " B");

export function initSettings(ctx) {
  C = ctx;
  const win = document.createElement("div");
  win.id = "setWin";
  win.className = "setview hidden";
  win.innerHTML = `
    <div class="sv-top"><button class="sv-back" id="swClose" title="返回（Esc）">← 返回</button><span class="sw-tip" id="swTip"></span></div>
    <div class="sv-body">
      <nav class="sv-nav" id="swNav"></nav>
      <aside class="sv-sub hidden" id="swSub"></aside>
      <section class="sv-main" id="swMain"></section>
    </div>`;
  document.body.appendChild(win);
  $("#swClose").onclick = closeSettings;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && settingsOpen() && !e.target.closest?.("input,textarea")) closeSettings(); });
}

export function settingsOpen() { return !$("#setWin").classList.contains("hidden"); }
export function closeSettings() { $("#setWin").classList.add("hidden"); document.body.classList.remove("in-settings"); document.querySelector('[data-panel="settings"]')?.classList.remove("on"); }
export async function toggleSettings(force, which) {
  const open = force ?? !settingsOpen();
  if (!open) return closeSettings();
  if (which) { const [p1, s1] = String(which).split("/"); if (GROUPS.some(([, x]) => x.some(([k]) => k === p1))) page = p1; if (s1) sub[p1] = s1; }
  $("#setWin").classList.remove("hidden");
  document.body.classList.add("in-settings");
  document.querySelector('[data-panel="settings"]')?.classList.add("on");
  await reload();
}

async function reload() {
  const [c, p] = await Promise.all([C.safe(() => C.api("/api/settings")), presets.length ? presets : C.safe(() => C.api("/api/providers/presets"))]);
  if (!c) return;
  cfg = c; presets = p || [];
  if (!selId && cfg.providers.length) selId = cfg.providers[0].id;
  $("#swTip").innerHTML = cfg.secrets_encrypted ? (cfg.secrets_unlocked ? "机密已加密 · 已解锁" : '<span class="warn">机密已加密，尚未解锁</span>') : "";
  renderNav(); render();
}

function renderNav() {
  const f = navFilter.trim();
  $("#swNav").innerHTML = `<div class="sv-navhead"><b>设置</b><button class="sv-ico" id="svFind" title="搜索设置">⌕</button></div>
    <input id="svSearch" class="sv-search ${f || $("#svSearch")?.dataset.open ? "" : "hidden"}" placeholder="搜索设置…" value="${E(f)}">
    ${GROUPS.map(([g, items], gi) => {
      const its = items.filter(([, , t]) => !f || t.includes(f) || g.includes(f));
      if (!its.length) return "";
      return `${gi ? `<div class="sv-group">${g}</div>` : ""}${its.map(([k, i, t]) => `<button data-p="${k}" class="sv-item ${k === page ? "on" : ""}"><span class="sv-i">${i}</span>${t}</button>`).join("")}`;
    }).join("")}`;
  $("#swNav").querySelectorAll(".sv-item").forEach((b) => (b.onclick = () => { page = b.dataset.p; renderNav(); render(); }));
  $("#svFind").onclick = () => { const s = $("#svSearch"); s.classList.toggle("hidden"); s.dataset.open = s.classList.contains("hidden") ? "" : "1"; s.focus(); };
  $("#svSearch").oninput = (e) => { navFilter = e.target.value; const pos = e.target.selectionStart; renderNav(); const s = $("#svSearch"); s.classList.remove("hidden"); s.focus(); s.setSelectionRange(pos, pos); };
}

function renderSub() {
  const el = $("#swSub"), groups = SUBS[page];
  if (!groups) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  sub[page] = sub[page] || groups[0][1][0][0];
  const title = GROUPS.flatMap(([, x]) => x).find(([k]) => k === page)[2];
  el.innerHTML = `<div class="sv-subhead">${title}</div>` + groups.map(([g, items]) => `${g ? `<div class="sv-group">${g}</div>` : ""}${items.map(([k, i, t, soon]) =>
    `<button data-s="${k}" class="sv-item ${k === sub[page] ? "on" : ""} ${soon ? "soon" : ""}" ${soon ? "disabled title=\"即将支持\"" : ""}><span class="sv-i">${i}</span>${t}${soon ? '<small class="sv-soon">即将支持</small>' : ""}</button>`).join("")}`).join("");
  el.querySelectorAll("[data-s]:not([disabled])").forEach((b) => (b.onclick = () => { sub[page] = b.dataset.s; renderSub(); renderMain(); }));
}

function render() { renderSub(); renderMain(); }

function renderMain() {
  const m = $("#swMain");
  m.className = "sv-main" + (page === "providers" ? " three" : "");
  m.scrollTop = 0;
  ({ profile: pageProfile, providers: pageProviders, defaults: pageDefaults, embed: pageEmbed, mcp: pageMcp, skills: pageSkills,
     watch: pageWatch, eye: pageEye, voice: pageVoice, general: pageGeneral, appearance: pageAppearance, data: pageData,
     core: pageCore, about: pageAbout })[page](m);
}

// ———————————————— 卡片组件 + 自动保存 ————————————————
const card = (title, body, extra = "") => `<div class="sv-card" ${extra}><div class="sv-card-h">${title}</div>${body}</div>`;
const row = (label, control, help = "", cls = "") => `<div class="sv-row ${cls}"><div class="sv-row-main"><div class="sv-row-l">${label}</div><div class="sv-row-c">${control}</div></div>${help ? `<div class="sv-help">${help}</div>` : ""}</div>`;
const val = (path) => { const [a, b] = path.split("."); return cfg[a]?.[b]; };
const tog = (path) => `<label class="switch"><input type="checkbox" data-k="${path}" ${val(path) ? "checked" : ""}><span></span></label>`;
const inp = (path, type = "text", ph = "", w = "") => {
  const [a, b] = path.split(".");
  if (type === "secret") return `<input class="sv-in ${w}" data-k="${path}" type="password" value="" placeholder="${cfg[a]?.[b + "_set"] ? "已设置（留空不修改）" : "未设置"}">`;
  return `<input class="sv-in ${w}" data-k="${path}" type="${type}" value="${E(val(path) ?? "")}" placeholder="${E(ph)}">`;
};
const sel = (path, opts, w = "") => `<select class="sv-in ${w}" data-k="${path}">${opts.map(([v, t]) => `<option value="${E(v)}" ${String(val(path)) === String(v) ? "selected" : ""}>${E(t)}</option>`).join("")}</select>`;
const seg = (path, opts) => `<div class="seg" data-seg="${path}">${opts.map(([v, t]) => `<button data-v="${E(v)}" class="${String(val(path)) === String(v) ? "on" : ""}">${t}</button>`).join("")}</div>`;

async function savePatch(patch) {
  cfg = await C.api("/api/settings", { method: "PUT", body: patch });
  flash("已自动保存");
  if (patch.ui) window.StarTheme?.apply(cfg.ui);
  return cfg;
}
function bindAuto(m) {
  m.querySelectorAll("[data-k]").forEach((el) => {
    el.onchange = () => C.safe(async () => {
      const [a, b] = el.dataset.k.split(".");
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "number") v = v === "" ? 0 : Number(v);
      if (el.type === "password" && v === "") return;
      await savePatch({ [a]: { [b]: v } });
      if (el.type === "password") { el.value = ""; el.placeholder = "已设置（留空不修改）"; }
      C.refresh?.();
    });
  });
  m.querySelectorAll("[data-seg]").forEach((g) => g.querySelectorAll("button").forEach((b) => (b.onclick = () => C.safe(async () => {
    const [a, k] = g.dataset.seg.split(".");
    g.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    await savePatch({ [a]: { [k]: b.dataset.v } });
  }))));
}
function flash(t) { const el = $("#swTip"); if (!el) return; el.textContent = t; el.classList.add("pulse"); setTimeout(() => el.classList.remove("pulse"), 900); }

// ———————————————— 保存（模型服务） ————————————————
function queueSave(delay = 500) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => C.safe(saveProviders), delay);
}
async function saveProviders() {
  clearTimeout(saveTimer); saveTimer = null;
  const providers = cfg.providers.map(({ api_key_set, ...p }) => p);
  const r = await C.api("/api/settings", { method: "PUT", body: { providers, defaults: cfg.defaults } });
  const byId = new Map(r.providers.map((x) => [x.id, x]));
  cfg.providers.forEach((x) => { const y = byId.get(x.id); if (y) { x.api_key = y.api_key; x.api_key_set = y.api_key_set; } });
  const { providers: _p, defaults: _d, ...rest } = r; Object.assign(cfg, rest);
  flash("已自动保存");
}

// ———————————————— 模型服务 ————————————————
function cur() { return cfg.providers.find((p) => p.id === selId); }
function presetOf(p) { return presets.find((x) => x.preset === p.preset) || {}; }

function pageProviders(m) {
  const list = cfg.providers.filter((p) => !provFilter || (p.name + p.base_url).toLowerCase().includes(provFilter.toLowerCase()));
  m.innerHTML = `
    <div class="sw-col">
      <input id="pvSearch" type="search" placeholder="搜索模型平台…" value="${E(provFilter)}">
      <div class="pv-list">${list.map((p) => `<div class="pv-item ${p.id === selId ? "on" : ""}" data-id="${E(p.id)}">
          <span class="pv-logo">${E((p.name || "?").slice(0, 1))}</span><span class="pv-name">${E(p.name)}</span>
          <span class="dot ${p.enabled ? (p.api_key_set || p.api_key || p.preset === "ollama" ? "ok" : "half") : ""}" title="${p.enabled ? "已启用" : "已关闭"}"></span></div>`).join("")
        || '<div class="sw-empty">还没有服务商。点下方「添加服务商」，选一家、填上 Key 就行。</div>'}</div>
      <button class="pv-add" id="pvAdd">＋ 添加服务商</button>
    </div>
    <div class="sw-detail" id="pvDetail"></div>`;
  $("#pvSearch").oninput = (e) => { provFilter = e.target.value; const pos = e.target.selectionStart; pageProviders(m); const s = $("#pvSearch"); s.focus(); s.setSelectionRange(pos, pos); };
  m.querySelectorAll(".pv-item").forEach((el) => (el.onclick = () => { selId = el.dataset.id; modelFilter = ""; pageProviders(m); }));
  $("#pvAdd").onclick = () => addPicker();
  detail();
}

function addPicker() {
  const d = $("#pvDetail");
  d.innerHTML = `<h2>添加服务商</h2><div class="sub">选一家，地址已经帮你填好，只需要填 API Key。没有的选「自定义」。</div>
    <div class="preset-grid">${presets.map((p) => `<button data-p="${p.preset}"><b>${E(p.name)}</b><small>${E(p.base_url || "自己填地址")}</small></button>`).join("")}</div>`;
  d.querySelectorAll("[data-p]").forEach((b) => (b.onclick = () => {
    const ps = presets.find((x) => x.preset === b.dataset.p);
    const p = { id: uid(ps.preset), name: ps.name, preset: ps.preset, protocol: ps.protocol, base_url: ps.base_url, api_key: "", enabled: true, models: [] };
    cfg.providers.push(p); selId = p.id; C.safe(saveProviders).then(() => { pageProviders($("#swMain")); $("#pvKey")?.focus(); });
  }));
}

function endpointPreview(p) {
  const b = (p.base_url || "").replace(/\/+$/, "");
  if (!b) return "填写后这里会显示实际请求的地址";
  if (p.protocol === "anthropic") return `${b}${/\/v1$/.test(b) ? "" : "/v1"}/messages`;
  if (p.protocol === "responses") return `${b}/responses`;
  return `${b}/chat/completions`;
}

function detail() {
  const d = $("#pvDetail"), p = cur();
  if (!p) { d.innerHTML = '<div class="sw-empty big">← 选择或添加一个服务商</div>'; return; }
  const ps = presetOf(p);
  const groups = {};
  (p.models || []).filter((x) => !modelFilter || x.id.toLowerCase().includes(modelFilter.toLowerCase()))
    .forEach((x) => (groups[x.kind || "chat"] = groups[x.kind || "chat"] || []).push(x));
  const isDef = (mid) => ROLES.filter(([r]) => cfg.defaults[r] === `${p.id}::${mid}`).map(([, t]) => t.replace(/（.*）/, ""));
  d.innerHTML = `
    <div class="pv-top"><input class="pv-title" id="pvName" value="${E(p.name)}" title="点击改名">
      <select id="pvProto" title="接口格式：Chat Completions / Anthropic Messages / OpenAI Responses"><option value="openai" ${!["anthropic", "responses"].includes(p.protocol) ? "selected" : ""}>OpenAI 兼容（Chat）</option><option value="responses" ${p.protocol === "responses" ? "selected" : ""}>OpenAI Responses</option><option value="anthropic" ${p.protocol === "anthropic" ? "selected" : ""}>Anthropic</option></select>
      <label class="switch" title="启用 / 关闭"><input type="checkbox" id="pvOn" ${p.enabled ? "checked" : ""}><span></span></label>
      <button class="icon danger" id="pvDel" title="删除这个服务商">🗑</button></div>
    <div class="f-label">API 密钥 ${ps.key_url ? `<a href="${E(ps.key_url)}" target="_blank" rel="noopener">获取密钥 ↗</a>` : ""}</div>
    <div class="f-row"><input id="pvKey" type="password" autocomplete="off" placeholder="${p.api_key_set ? "已保存（" + E(p.api_key) + "），重新填写会覆盖" : p.preset === "ollama" ? "本机 Ollama 不需要 Key" : "粘贴 API Key"}">
      <button class="icon" id="pvEye" title="显示 / 隐藏">👁</button><button id="pvCheck" class="primary">检测</button></div>
    <div class="f-label">API 地址 ${ps.base_url && ps.base_url !== p.base_url ? '<a href="#" id="pvReset">恢复默认</a>' : ""}</div>
    <div class="f-row"><input id="pvBase" value="${E(p.base_url)}" placeholder="https://…/v1"></div>
    <div class="f-hint">预览：<code id="pvPrev">${E(endpointPreview(p))}</code></div>
    <div id="pvMsg" class="pv-msg hidden"></div>
    <div class="pv-mhead"><b>模型</b><span class="muted">${(p.models || []).length} 个</span>
      <input id="pvMSearch" type="search" placeholder="筛选模型" value="${E(modelFilter)}">
      <button id="pvSync">⟳ 同步模型</button><button class="icon" id="pvMAdd" title="手动添加">＋</button></div>
    ${(p.models || []).length ? Object.entries(groups).sort(([a], [b]) => Object.keys(KIND).indexOf(a) - Object.keys(KIND).indexOf(b)).map(([k, arr]) => `
      <details class="mgroup" open><summary>${KIND[k] || k} <span class="muted">${arr.length}</span></summary>
      ${arr.map((x) => `<div class="mrow" data-m="${E(x.id)}"><span class="mname" title="${E(x.id)}">${E(x.id)}</span>
        ${isDef(x.id).map((t) => `<span class="tag on">${E(t)}</span>`).join("")}
        <select class="mkind" title="类型">${Object.entries(KIND).map(([v, t]) => `<option value="${v}" ${v === (x.kind || "chat") ? "selected" : ""}>${t}</option>`).join("")}</select>
        <select class="mdef" title="设为默认"><option value="">设为默认…</option>${ROLES.map(([r, t]) => `<option value="${r}">${t}</option>`).join("")}</select>
        <button class="mchk" title="发一句话试试">检测</button><button class="icon mdel" title="移除">−</button>
        <span class="mres"></span></div>`).join("")}</details>`).join("")
      : `<div class="sw-empty">还没有模型。填好 Key 后点「同步模型」，会自动从服务商拉取全部可用模型。</div>`}`;

  $("#pvName").onchange = (e) => { p.name = e.target.value.trim() || p.name; queueSave(0); setTimeout(() => pageProviders($("#swMain")), 200); };
  $("#pvProto").onchange = (e) => { p.protocol = e.target.value; $("#pvPrev").textContent = endpointPreview(p); queueSave(); };
  $("#pvOn").onchange = (e) => { p.enabled = e.target.checked; queueSave(0); setTimeout(() => pageProviders($("#swMain")), 200); };
  $("#pvDel").onclick = () => confirm(`删除「${p.name}」？用到它的默认模型会被清空。`) && C.safe(async () => {
    cfg.providers = cfg.providers.filter((x) => x.id !== p.id);
    Object.keys(cfg.defaults).forEach((r) => { if ((cfg.defaults[r] || "").startsWith(p.id + "::")) cfg.defaults[r] = ""; });
    selId = cfg.providers[0]?.id || ""; await saveProviders(); pageProviders($("#swMain"));
  });
  $("#pvEye").onclick = () => { const k = $("#pvKey"); k.type = k.type === "password" ? "text" : "password"; };
  $("#pvKey").onchange = (e) => { const v = e.target.value.trim(); if (v) { p.api_key = v; p.api_key_set = true; queueSave(0); } };
  $("#pvBase").oninput = (e) => { p.base_url = e.target.value.trim(); $("#pvPrev").textContent = endpointPreview(p); queueSave(800); };
  if ($("#pvReset")) $("#pvReset").onclick = (e) => { e.preventDefault(); p.base_url = ps.base_url; queueSave(0); detail(); };
  $("#pvMSearch").oninput = (e) => { modelFilter = e.target.value; const pos = e.target.selectionStart; detail(); const s = $("#pvMSearch"); s.focus(); s.setSelectionRange(pos, pos); };
  $("#pvCheck").onclick = () => checkProvider(p);
  $("#pvSync").onclick = () => syncModels(p);
  $("#pvMAdd").onclick = () => { const id = (prompt("模型 ID（和服务商文档里写的一致）") || "").trim(); if (!id) return;
    if (!(p.models || []).some((x) => x.id === id)) { p.models = [...(p.models || []), { id, kind: guessKind(id) }]; queueSave(0); detail(); } };
  d.querySelectorAll(".mrow").forEach((row) => {
    const mid = row.dataset.m, mm = p.models.find((x) => x.id === mid);
    row.querySelector(".mkind").onchange = (e) => { mm.kind = e.target.value; queueSave(); };
    row.querySelector(".mdef").onchange = (e) => { if (!e.target.value) return; cfg.defaults[e.target.value] = `${p.id}::${mid}`;
      C.safe(saveProviders).then(() => { C.toast(`已设为默认${ROLES.find(([r]) => r === e.target.value)[1]}：${mid}`); detail(); C.refresh?.(); }); };
    row.querySelector(".mdel").onclick = () => { p.models = p.models.filter((x) => x.id !== mid); queueSave(0); detail(); };
    row.querySelector(".mchk").onclick = async (e) => {
      const res = row.querySelector(".mres"), btn = e.target; btn.disabled = true; res.className = "mres"; res.textContent = "检测中…";
      try { await flushKey(p); const r = await C.api("/api/providers/check", { method: "POST", body: { id: p.id, model: mid, kind: mm.kind } });
        res.className = "mres ok"; res.textContent = `✓ ${r.msg}${r.reply ? " · 「" + r.reply + "」" : ""}`;
      } catch (err) { res.className = "mres bad"; res.textContent = "✗ " + err.message; }
      btn.disabled = false;
    };
  });
}

async function flushKey(p) { if (saveTimer) await saveProviders(); }

function say(ok, html) { const el = $("#pvMsg"); if (!el) return; el.className = "pv-msg " + (ok ? "ok" : "bad"); el.innerHTML = html; }

async function checkProvider(p) {
  const btn = $("#pvCheck"); btn.disabled = true; btn.textContent = "检测中…";
  const typed = $("#pvKey").value.trim(); if (typed) { p.api_key = typed; p.api_key_set = true; }
  try {
    await saveProviders();
    const r = await C.api("/api/providers/check", { method: "POST", body: { id: p.id } });
    if (r.fixed_base_url) { p.base_url = r.fixed_base_url; await saveProviders(); }
    say(true, `✓ ${E(r.msg)}${r.fixed_base_url ? `<br>地址少了 /v1，已自动改成 <code>${E(r.fixed_base_url)}</code>` : ""}`);
    if (!(p.models || []).length) await syncModels(p, true);
  } catch (e) { say(false, "✗ " + E(e.message)); }
  if ($("#pvCheck")) { $("#pvCheck").disabled = false; $("#pvCheck").textContent = "检测"; }
}

async function syncModels(p, quiet = false) {
  const btn = $("#pvSync"); if (btn) { btn.disabled = true; btn.textContent = "同步中…"; }
  const typed = $("#pvKey")?.value.trim(); if (typed) { p.api_key = typed; p.api_key_set = true; }
  try {
    await saveProviders();
    const r = await C.api("/api/providers/models", { method: "POST", body: { id: p.id } });
    const old = new Map((p.models || []).map((x) => [x.id, x]));
    p.models = r.models.filter((x) => x.kind !== "other" || old.has(x.id)).map((x) => old.get(x.id) || x);
    if (r.fixed_base_url) p.base_url = r.fixed_base_url;
    // 还没有对话 / 向量默认模型时，顺手挑一个，省得再去「默认模型」里选
    const pickFirst = (role, kinds) => { if (!cfg.defaults[role]) { const m = p.models.find((x) => kinds.includes(x.kind)); if (m) cfg.defaults[role] = `${p.id}::${m.id}`; } };
    pickFirst("chat", ["chat", "vision"]); pickFirst("vision", ["vision"]); pickFirst("embed", ["embedding"]);
    await saveProviders();
    detail();
    say(true, `✓ 已同步 ${p.models.length} 个模型${r.fixed_base_url ? `（地址已自动补上 /v1）` : ""}。在模型右侧「设为默认…」里选，或到「默认模型」统一设置。`);
  } catch (e) { if (!quiet) say(false, "✗ 同步失败：" + E(e.message)); else say(false, "✗ Key 有效，但拉取模型列表失败：" + E(e.message) + "。可以点「＋」手动添加模型。"); }
  if ($("#pvSync")) { $("#pvSync").disabled = false; $("#pvSync").textContent = "⟳ 同步模型"; }
}

function guessKind(id) {
  if (/embed|bge|m3e|gte-|e5-/i.test(id)) return "embedding";
  if (/vl|vision|omni|4o|gemini|claude|glm-4v|qvq/i.test(id)) return "vision";
  return "chat";
}

// ———————————————— 默认模型 ————————————————
function pageDefaults(m) {
  const opts = (kinds, val) => {
    const groups = cfg.providers.filter((p) => p.enabled).map((p) => {
      const ms = (p.models || []).filter((x) => kinds.includes(x.kind || "chat"));
      return ms.length ? `<optgroup label="${E(p.name)}">${ms.map((x) => `<option value="${E(p.id + "::" + x.id)}" ${val === p.id + "::" + x.id ? "selected" : ""}>${E(x.id)}</option>`).join("")}</optgroup>` : "";
    }).join("");
    const missing = val && !groups.includes(`value="${E(val)}"`) ? `<option value="${E(val)}" selected>（已失效）${E(val.split("::")[1] || val)}</option>` : "";
    return `<option value="">${kinds.includes("embedding") ? "不使用" : "未选择"}</option>${missing}${groups}`;
  };
  m.innerHTML = `<div class="sv-card"><div class="sv-card-h">默认模型</div><div class="sv-help" style="margin-top:-4px">从已同步的模型里选，不用手打模型名。下拉里没有想要的？先到「模型服务」同步或手动添加。</div>
    ${ROLES.map(([r, t, desc, kinds]) => `<div class="def-row"><div><b>${t}</b><div class="muted">${desc}</div></div>
      <div class="f-row"><select data-role="${r}">${opts(kinds, cfg.defaults[r] || "")}</select><button data-chk="${r}">检测</button></div>
      <div class="mres" id="dr-${r}"></div></div>`).join("")}
    ${cfg.providers.length ? "" : '<div class="sw-empty">还没有服务商：先到「模型服务」添加一家并填 Key。</div>'}</div>
    ${contextCard()}`;
  bindAuto(m);
  m.querySelectorAll("select[data-role]").forEach((s) => (s.onchange = () => { cfg.defaults[s.dataset.role] = s.value; C.safe(saveProviders).then(() => C.refresh?.()); }));
  m.querySelectorAll("[data-chk]").forEach((b) => (b.onclick = async () => {
    const r = b.dataset.chk, out = $("#dr-" + r); out.className = "mres"; out.textContent = "检测中…"; b.disabled = true;
    try { if (saveTimer) await saveProviders(); const x = await C.api("/api/providers/check", { method: "POST", body: { role: r } });
      out.className = "mres ok"; out.textContent = `✓ ${x.msg}${x.reply ? " · 「" + x.reply + "」" : ""}`;
    } catch (e) { out.className = "mres bad"; out.textContent = "✗ " + e.message; }
    b.disabled = false;
  }));
}

function contextCard() {
  return card("上下文窗口与自动压缩",
    row("主模型上下文窗口（tokens）", inp("llm.context_tokens", "number", "400000", "w-s"), "对话抽屉、整理星系能看到的上下文上限，默认 400,000。对话监控刻意不带上下文，只看一轮。") +
    row("整理模型上下文窗口（tokens）", inp("llm.maint_context_tokens", "number", "400000", "w-s")) +
    row("自动压缩阈值（tokens）", inp("llm.compact_tokens", "number", "380000", "w-s"), "累计上下文到这么多时自动压缩：较早的历史压成一段摘要，保留最近几轮。每次压缩都会出现在任务抽屉里。"));
}

// ———————————————— 个人信息（主人 + 安全） ————————————————
async function pageProfile(m) {
  const core = await C.safe(() => C.api("/api/core")) || {};
  const owner = core.sections?.owner?.content || "";
  m.innerHTML = card("主人", row("关于主人（核心记忆 · 第二段）", `<a class="sv-link" href="/core">去核心记忆编辑 ↗</a>`,
      owner ? E(owner).slice(0, 160).replace(/\n/g, "<br>") : "还没有写。和 AI 聊天时说「记住，以后叫我…」也会记进来。") +
    row("主人令牌", inp("core.owner_token", "secret", "", "w-m"), "远程（不在本机）调用 API / 云服务器同步时用它证明是你；本机访问不需要。")) +
    card("安全", `<div class="sv-help" style="margin:0 0 8px">用主密码加密 API Key、WebDAV 密码、备份口令。启用后每次启动需在这里解锁（或设置环境变量 STARWISH_MASTER）。</div>
      ${row("状态", cfg.secrets_encrypted ? (cfg.secrets_unlocked ? '<span class="okc">已加密 · 已解锁</span>' : '<span class="warn">已加密 · 尚未解锁</span>') : '<span class="muted">明文保存在 soul/config.json</span>')}
      ${row("主密码", `<input id="master" class="sv-in w-m" type="password" placeholder="主密码">${cfg.secrets_encrypted ? '<button id="mUnlock" class="primary">解锁</button><button class="danger" id="mOff">关闭加密</button>' : '<button id="mOn" class="primary">启用加密</button>'}`)}`);
  bindAuto(m);
  const go = (path, body, msg) => C.safe(async () => { await C.api(path, { method: "POST", body }); C.toast(msg); reload(); });
  if ($("#mOn")) $("#mOn").onclick = () => go("/api/secrets/encrypt", { passphrase: $("#master").value }, "已启用加密");
  if ($("#mUnlock")) $("#mUnlock").onclick = () => go("/api/secrets/unlock", { passphrase: $("#master").value }, "已解锁");
  if ($("#mOff")) $("#mOff").onclick = () => confirm("关闭加密后机密将以明文保存，确认？") && go("/api/secrets/decrypt", {}, "已关闭加密");
}

// ———————————————— 本地模型（向量） ————————————————
function pageEmbed(m) {
  m.innerHTML = card("向量模型（语义检索）",
    `<div class="sv-help" style="margin:0 0 6px">和 jieba + FTS5 一起找记忆：字面没对上也能按意思找到（搜「保暖外套」能找到「羽绒服」）。选的档位没准备好时自动用内置模型。</div>` +
    row("启用语义检索", tog("memory.semantic")) +
    row("使用哪个向量模型", sel("embed.provider", [["small", "① 内置 bge-small-zh int8（23.9MB）"], ["m3", "② BGE-M3 int8（约 590MB · 一键部署）"], ["api", "③ 接口（默认模型里选）"]], "w-l")) +
    `<div id="embBox" class="sv-help">正在读取向量模型状态…</div>`) +
    card("下载与高级", row("模型下载镜像", inp("embed.hf_mirror", "text", "https://hf-mirror.com", "w-l")) +
      row("先走镜像", tog("embed.prefer_mirror"), "国内网络建议打开") + row("③ 向量维度（0 = 服务默认）", inp("embed.api_dimensions", "number", "", "w-s")));
  bindAuto(m);
  C.embedPanel();
}

// ———————————————— MCP 与代理 ————————————————
function pageMcp(m) {
  const o = location.origin;
  m.innerHTML = card("接入地址", row("OpenAI 兼容", `<code>${o}/v1</code>`, "聊天客户端把 API 地址改成它，记忆就会自动注入。") +
      row("REST API", `<code>${o}/api</code>`) + row("MCP", `<code id="mcpCmd">MCP接入.bat（或 python mcp_server.py）</code>`, "在 Claude / Cursor 等客户端的 MCP 设置里填这条命令。")) +
    card("对话代理", row("自动注入核心记忆 + 相关星星", tog("proxy.inject")) + row("把对话写入归档，供日后整理", tog("proxy.log_to_archive")) +
      row("每轮召回预算（tokens）", inp("proxy.budget_tokens", "number", "", "w-s")) + row("强制模型（可空）", inp("proxy.model_override", "text", "", "w-m")) +
      row("代理密钥", inp("proxy.proxy_key", "secret", "", "w-m"), "可空；填了则客户端需用它作为 API Key。"));
  bindAuto(m);
  fetch("/api/health").then((r) => r.json()).then((h) => { const el = $("#mcpCmd"); if (el && h.exe) el.textContent = `"${h.exe}" --mcp`; }).catch(() => {});
}

// ———————————————— 技能 ————————————————
function pageSkills(m) {
  m.innerHTML = card("技能进化", row("打开技能页", `<a class="sv-link" href="/skills">技能进化 ↗</a>`) +
    row("自进化总开关", tog("evolve.enabled")) +
    row("AI 修改技能", sel("evolve.approval", [["auto", "安全的直接生效，危险的交我批准"], ["always", "全部要我批准"]], "w-l")) +
    row("后台巡检间隔（轮对话，0 = 关）", inp("evolve.nudge_interval", "number", "", "w-s")) +
    row("巡检时顺便提取星星和核心提案", tog("evolve.review_memory")) + row("代理里注入相关技能", tog("evolve.inject")) +
    row("对话监控：任务闭环时可复用的流程存成技能", tog("watch.skill_on_close")));
  bindAuto(m);
}

// ———————————————— 对话监控 ————————————————
async function pageWatch(m) {
  const st = await C.safe(() => C.api("/api/watch/status"));
  if (!st) return;
  const s = st.status || {};
  const clientRow = (x) => `<div class="sv-row wc" data-c="${x.id}"><div class="sv-row-main">
      <div class="sv-row-l"><b>${E(x.title)}</b>${x.experimental ? ' <span class="tag on">实验性</span>' : ""} ${x.exists ? '<span class="tag">已找到记录</span>' : '<span class="tag dim">未找到记录目录</span>'}</div>
      <div class="sv-row-c"><span class="muted">监控</span><label class="switch"><input type="checkbox" class="wc-on" ${x.enabled ? "checked" : ""}><span></span></label>
        <span class="muted">钩子</span><label class="switch" title="${E(x.hook_target || "")}"><input type="checkbox" class="wc-hook" ${x.hook_installed ? "checked" : ""}><span></span></label></div></div>
      <div class="sv-help">记录位置：${x.roots.length ? x.roots.map((r) => `<code>${E(r)}</code>`).join(" ") : "需要填写路径"} · 已盯 ${x.stats?.files ?? 0} 个会话 · 已归纳 ${x.stats?.turns ?? 0} 轮${x.stats?.last_error ? ` · <span class="warn">${E(x.stats.last_error)}</span>` : ""}<br>
        格式核实：${E(x.verified)}</div>
      <details class="more"><summary>路径覆盖 / 钩子命令</summary>
        <div class="f-row" style="margin-top:6px"><input class="sv-in wc-path" placeholder="留空 = 默认位置；多个用 ; 分隔" value="${E(x.path || "")}"><button class="wc-save">保存路径</button></div>
        <div class="sv-help">钩子命令：<code>${E(x.hook_command)}</code>${x.hook_target ? `<br>写入：<code>${E(x.hook_target)}</code>（改之前自动备份，关掉开关即干净卸载）` : ""}</div></details></div>`;
  m.innerHTML = card("对话监控",
    `<div class="sv-help" style="margin:0 0 6px">不经过代理：直接读各 AI 客户端写在本机的对话记录。每轮结束后，监控 AI（不带上下文，只看这一轮）判断有没有值得记的——普通偏好 / 事实直接成星，重要偏好变成核心提案，没做完的任务记成「未闭环」碎片，做完后合并成一个可复用的任务。关着软件时聊的，下次打开会补上。</div>` +
    row("总开关", `<label class="switch"><input type="checkbox" id="wAll" ${st.enabled ? "checked" : ""}><span></span></label>`) +
    row("立即归纳", `<button class="primary" id="wNow">立即归纳</button>`, `上次运行：${fmtT(s.last_run)} · 本次启动已归纳 ${s.turns || 0} 轮、跳过琐碎 ${s.skipped || 0} 轮 · 队列 ${s.queue || 0}` +
      (st.llm_ready ? "" : '<br><span class="warn">还没有配置整理模型（默认模型 → 整理模型 / 对话模型），对话先记着位置，配好后自动补上。</span>') +
      (s.last_error ? `<br><span class="warn">最近错误：${E(s.last_error)}</span>` : ""))) +
    card("客户端", `<div class="sv-help" style="margin:0 0 4px">「钩子」= 在客户端里装一个「本轮结束」提醒，捕获更及时；不装也行，轮询（几秒看一次文件大小）同样能捕获。</div>` + st.clients.map(clientRow).join("")) +
    card("高级", row("轮询间隔（秒）", inp("watch.poll_sec", "number", "", "w-s"), "只看文件大小和修改时间，不读内容，几乎不占资源。") +
      row("静止多久算一轮结束（秒）", inp("watch.idle_sec", "number", "", "w-s")) + row("只盯最近几天的会话", inp("watch.hot_days", "number", "", "w-s")) +
      row("偏好重要度 ≥ 多少转成核心提案", inp("watch.core_importance", "number", "", "w-s"), "自动通道绝不直接改核心记忆。") +
      row("第一次启用时回读最近几天的旧对话", tog("watch.backfill_existing"), "默认从现在开始，不花钱回灌历史。") +
      row("归纳出新星后让主模型整理星系", tog("watch.organize_after")));
  bindAuto(m);
  const post = (cid, body) => C.safe(async () => { const r = await C.api(`/api/watch/client/${cid}`, { method: "POST", body });
    if (r.hook_result?.manual) alert(r.hook_result.help); else if (r.hook_result) C.toast(r.hook_result.removed !== undefined ? "钩子已卸载" : "钩子已安装：" + r.hook_result.path);
    flash("已保存"); pageWatch(m); });
  $("#wAll").onchange = (e) => post("claude", { watch_enabled: e.target.checked });
  $("#wNow").onclick = () => C.safe(async () => { $("#wNow").disabled = true; $("#wNow").textContent = "归纳中…";
    const r = await C.api("/api/watch/process", { method: "POST" }); C.toast(r.ok ? `归纳完成：${r.turns} 轮` : r.reason, !r.ok); pageWatch(m); });
  m.querySelectorAll(".wc").forEach((el) => {
    const cid = el.dataset.c;
    el.querySelector(".wc-on").onchange = (e) => post(cid, { enabled: e.target.checked });
    el.querySelector(".wc-hook").onchange = (e) => post(cid, { hook: e.target.checked });
    el.querySelector(".wc-save").onclick = () => post(cid, { path: el.querySelector(".wc-path").value });
  });
}

// ———————————————— 宇宙之眼 / 宇宙之声 ————————————————
function pageEye(m) {
  m.innerHTML = card("视频交互模型（宇宙之眼）", row("打开观看页", `<a class="sv-link" href="/eye">宇宙之眼 ↗</a>`) +
      row("引擎", sel("eye.engine", [["frame", "通用帧模式（任意识图模型）"], ["qwen_omni", "千问全模态视频接口"]], "w-l")) +
      row("通用帧模式的识图模型", `<b>${E(cfg.eye.frame_model || "未选择（沿用对话模型）")}</b> <a href="#" class="sv-link" id="goDef">去默认模型选择</a>`) +
      row("多帧怎么发", sel("eye.frame_compose", [["grid", "拼成一张分镜图（省钱）"], ["multi", "每帧一张图"]], "w-l")) +
      row("主动吐槽节奏", sel("eye.pace", [["off", "关"], ["calm", "安静"], ["normal", "正常"], ["chatty", "话多"]], "w-m")) +
      row("结束时沉淀成一颗星", tog("eye.star_on_end"))) +
    card("千问视频接口", row("协议", sel("eye.qwen_protocol", [["openai", "OpenAI 兼容"], ["anthropic", "Anthropic 兼容"]], "w-m")) +
      row("模型", inp("eye.qwen_model", "text", "", "w-m")) + row("API Key", inp("eye.qwen_api_key", "secret", "", "w-m")) +
      row("帧序列传法", sel("eye.qwen_video_mode", [["video", "按视频传（失败自动退回多图）"], ["images", "每帧一张图"]], "w-l")));
  bindAuto(m);
  $("#goDef").onclick = (e) => { e.preventDefault(); page = "defaults"; renderNav(); render(); };
}
function pageVoice(m) {
  m.innerHTML = card("宇宙之声（声纹插件）", `<div class="sv-help" style="margin:0 0 6px">看片时分辨片中每个人并认出名字。插件在观看页里一键下载 / 卸载。</div>` +
    row("启用", tog("voice.enabled")) + row("逐句转写并标上说话人", tog("voice.transcribe")) +
    row("让视觉模型顺便认人", tog("voice.visual_naming")) + row("按片名 / 系列记住认识的人", tog("voice.use_library")) +
    row("分清哪句是主人说的", tog("voice.owner_detect")) + row("模型下载加速前缀", inp("voice.download_mirror", "text", "https://ghfast.top/", "w-l")));
  bindAuto(m);
}

// ———————————————— 通用（记忆与星辰） ————————————————
async function pageGeneral(m) {
  const cats = await C.safe(() => C.api("/api/organize/categories")) || { categories: [] };
  const last = cats.last;
  const majors = [...new Set(cats.categories.map((x) => x.major))];
  m.innerHTML = card("星系整理（主模型）",
    `<div class="sv-help" style="margin:0 0 6px">星图是一个银河系：核心记忆是星系核，大类是恒星系，小类是行星系，每条记忆是一颗星球。主模型负责把内容相同的星融合、同类的星放到一起。</div>` +
    row("立即整理星系", `<button class="primary" id="orgGo">整理星系</button>`, last ? `上次：${fmtT(last.t)} · 融合 ${last.fused}，分类 ${last.assigned}，合并类别 ${last.merged}${last.by_llm ? "" : "（按标签粗分）"}` : "还没整理过") +
    row("恒星系", `<span class="muted">${majors.length} 个</span>`, majors.slice(0, 30).map((x) => `<span class="tag">${E(x)}</span>`).join(" ") || "—")) +
    card("记忆亮度", row("亮度半衰期（天）", inp("memory.half_life_days", "number", "", "w-s"), "越新的星越亮，老星只是变暗、不会消失。") +
      row("最低亮度", inp("memory.brightness_floor", "number", "", "w-s")) + row("亮度在排序中的权重", inp("memory.brightness_weight", "number", "", "w-s")) +
      row("默认召回预算（tokens）", inp("memory.recall_budget_default", "number", "", "w-s"))) +
    card("星辰融合", row("自动融合相似的记忆", tog("memory.fuse_auto")) + row("每新增几颗星检查一次", inp("memory.fuse_every_new", "number", "", "w-s")) +
      row("相似度门槛", inp("memory.fuse_threshold", "number", "", "w-s"))) +
    card("定时任务", row("LLM 整理归档对话（小时，0 = 关）", inp("schedules.consolidate_hours", "number", "", "w-s")) +
      row("本地快照（小时）", inp("schedules.snapshot_hours", "number", "", "w-s")) + row("本地快照保留数", inp("schedules.keep_local_snapshots", "number", "", "w-s")));
  bindAuto(m);
  $("#orgGo").onclick = () => C.safe(async () => { await C.api("/api/organize", { method: "POST" }); C.toast("已排队整理，任务抽屉里能看到进度"); });
}

// ———————————————— 外观 ————————————————
const ACCENTS = [["#00B96B", "绿"], ["#F5222D", "红"], ["#FA8C16", "橙"], ["#1677FF", "蓝"], ["#722ED1", "紫"]];
function pageAppearance(m) {
  const ui = cfg.ui || {};
  const prev = (k, t, ico) => `<button class="th-card ${ui.theme === k ? "on" : ""}" data-th="${k}"><div class="th-prev th-${k}"><i></i><b></b></div><div class="th-name">${ico} ${t}</div></button>`;
  m.innerHTML = card("主题", `<div class="th-grid">${prev("light", "浅色", "☀")}${prev("dark", "深色", "☾")}${prev("system", "系统", "▭")}</div>
      <div class="sv-sep"></div>` +
    row("主题颜色", `<div class="swatches">${ACCENTS.map(([c, n]) => `<button class="sw ${ui.accent?.toUpperCase() === c ? "on" : ""}" data-c="${c}" title="${n}" style="background:${c}"></button>`).join("")}
      <label class="sw custom" title="自定义颜色" style="background:${E(ui.accent || "#00B96B")}"><input type="color" id="accPick" value="${E(ui.accent || "#00B96B")}"></label>
      <input class="sv-in w-s mono" id="accHex" value="${E(ui.accent || "#00B96B")}" maxlength="7"></div>`)) +
    card("显示", row("缩放", `<div class="zoomer"><button id="zm">−</button><span id="zv">${ui.scale || 100}%</span><button id="zp">＋</button></div>`) +
      row("对话抽屉位置", seg("ui.chat_side", [["left", "左侧"], ["right", "右侧"]])) +
      row("任务列表位置", seg("ui.task_side", [["left", "左侧"], ["right", "右侧"]])));
  bindAuto(m);
  const setUi = (patch) => C.safe(async () => { await savePatch({ ui: patch }); pageAppearance(m); });
  m.querySelectorAll("[data-th]").forEach((b) => (b.onclick = () => setUi({ theme: b.dataset.th })));
  m.querySelectorAll(".sw[data-c]").forEach((b) => (b.onclick = () => setUi({ accent: b.dataset.c })));
  $("#accPick").onchange = (e) => setUi({ accent: e.target.value.toUpperCase() });
  $("#accHex").onchange = (e) => { const v = e.target.value.trim(); if (/^#[0-9a-f]{6}$/i.test(v)) setUi({ accent: v.toUpperCase() }); else C.toast("颜色格式：#RRGGBB", true); };
  const zoom = (d) => setUi({ scale: Math.max(70, Math.min(160, (ui.scale || 100) + d)) });
  $("#zm").onclick = () => zoom(-10); $("#zp").onclick = () => zoom(10);
}

// ———————————————— 数据 ————————————————
async function pageData(m) {
  const s = sub.data || "dir";
  if (s === "dir") return dataDir(m);
  if (s === "local") return dataLocal(m);
  if (s === "webdav") return dataWebdav(m);
  if (s === "cloud") return dataCloud(m);
  if (s === "import") return dataImport(m);
}
async function backupDialog(target, title) {
  const r = await C.api("/api/data/backup", { method: "POST", body: { target } });
  C.toast(`${title}完成` + (r.name ? "：" + r.name : "") + (r.warning ? " · " + r.warning : ""), !!r.warning);
  return r;
}
async function dataDir(m) {
  const info = await C.safe(() => C.api("/api/data/info")) || {};
  m.innerHTML = card("数据", row("数据备份与恢复", `<button id="dBk">↓ 备份</button><button id="dRs">↺ 恢复</button>`, "备份到本地备份文件夹（设了备份口令会加密）；恢复前会先自动做一次快照。云端在左侧「云备份设置」里。")) +
    card("数据目录",
      row("应用数据", `<span class="path">&lrm;${E(info.soul_dir)}&lrm;</span><button data-open="soul">打开目录</button>`) +
      row("应用日志", `<span class="path">&lrm;${E(info.logs_dir)}&lrm;</span><button data-open="logs">打开日志</button>`) +
      row("日志保留期", sel("data.log_days", [[7, "7 天"], [14, "14 天"], [30, "30 天"], [90, "90 天"], [0, "永久"]], "w-m"), "超过该期限的日志文件会自动删除：启动时执行一次，之后每 12 小时执行一次。") +
      row(`清除缓存 <span class="muted">（约 ${fmtSize(info.cache_bytes)}）</span>`, `<button id="dCache">清除缓存</button>`) +
      row("重置数据", `<button class="danger solid" id="dReset">重置</button>`, "清空全部星星、提案、任务与核心记忆（设置保留）。会先自动做一次快照，可以从「本地备份」恢复。"));
  bindAuto(m);
  m.querySelectorAll("[data-open]").forEach((b) => (b.onclick = () => C.safe(async () => { const r = await C.api("/api/data/open", { method: "POST", body: { which: b.dataset.open } }); C.toast("目录：" + r.path); })));
  $("#dBk").onclick = () => C.safe(() => backupDialog("local", "本地备份"));
  $("#dRs").onclick = () => { sub.data = "local"; render(); };
  $("#dCache").onclick = () => C.safe(async () => { const r = await C.api("/api/data/clear_cache", { method: "POST" }); C.toast("已清除 " + fmtSize(r.freed)); dataDir(m); });
  $("#dReset").onclick = () => { const v = prompt("重置会清空全部记忆（会先自动快照）。确认请输入：重置"); if (v === "重置") C.safe(async () => {
    const r = await C.api("/api/data/reset", { method: "POST", body: { confirm: "重置" } }); C.toast("已重置，安全快照：" + r.safety_snapshot); C.refresh?.(); }); };
}
async function restoreList(m, target, sel2) {
  const box = $(sel2);
  const r = await C.safe(() => C.api("/api/data/backups?target=" + target));
  if (!r) { box.innerHTML = '<div class="sv-help">读取失败</div>'; return; }
  box.innerHTML = r.items.length ? r.items.slice(0, 20).map((x) => row(`<span class="mono">${E(x.name)}</span>`, `<span class="muted">${fmtSize(x.size)}</span><button data-rs="${E(x.name)}">恢复</button>`)).join("")
    : '<div class="sv-help">还没有备份</div>';
  box.querySelectorAll("[data-rs]").forEach((b) => (b.onclick = () => confirm(`用 ${b.dataset.rs} 覆盖当前数据？（会先自动快照）`) && C.safe(async () => {
    const pw = b.dataset.rs.endsWith(".enc") ? (prompt("备份口令（留空 = 用设置里的）") || "") : "";
    await C.api("/api/data/restore", { method: "POST", body: { target, name: b.dataset.rs, passphrase: pw, confirm: true } }); C.toast("已恢复"); C.refresh?.(); })));
}
async function dataLocal(m) {
  const info = await C.safe(() => C.api("/api/data/info")) || {};
  m.innerHTML = card("本地备份", row("备份文件夹", inp("data.local_dir", "text", info.local_dir || "", "w-l"), "留空 = 软件旁边的 soul/snapshots。可以填 U 盘或网盘同步文件夹。") +
      row("立即备份", `<button class="primary" id="lBk">备份</button><button data-open="local">打开文件夹</button>`, "完整快照 zip；设了备份口令（WebDAV 页）就加密。")) +
    card("恢复", `<div id="lList" class="sv-help">读取中…</div>`);
  bindAuto(m);
  $("#lBk").onclick = () => C.safe(async () => { await backupDialog("local", "本地备份"); restoreList(m, "local", "#lList"); });
  m.querySelector("[data-open]").onclick = () => C.safe(async () => { const r = await C.api("/api/data/open", { method: "POST", body: { which: "local" } }); C.toast("目录：" + r.path); });
  restoreList(m, "local", "#lList");
}
async function dataWebdav(m) {
  const info = await C.safe(() => C.api("/api/data/info")) || { last: {} };
  m.innerHTML = card("WebDAV", row("WebDAV 地址", inp("webdav.url", "text", "https://dav.example.com/dav/", "w-l")) +
      row("账号", inp("webdav.user", "text", "", "w-m")) + row("密码 / 应用密码", inp("webdav.password", "secret", "", "w-m")) +
      row("远端目录", inp("webdav.remote_dir", "text", "starwish", "w-m")) + row("完整备份保留版本数", inp("webdav.keep_versions", "number", "", "w-s")) +
      row("备份口令", inp("webdav.passphrase", "secret", "", "w-m"), "上传前用它加密（本地备份也用它），请牢记；忘了就解不开。")) +
    card("自动增量备份", row("每天 03:00（北京时间）自动上传", tog("data.webdav_daily"), "只传上次成功之后新增 / 改过 / 隐藏的记忆。关着软件错过了 03:00，下次打开会补上一次。每次都会出现在任务抽屉里。") +
      row("上次增量", `<span class="muted">${fmtT(info.last?.last_webdav_delta)}</span>`, info.last?.daily_webdav_t_fail && info.last.daily_webdav_t_fail > (info.last.daily_webdav_t || 0) ? '<span class="warn">最近一次自动上传失败，下一分钟会重试</span>' : "") +
      row("立即操作", `<button class="primary" id="wDelta">增量上传</button><button id="wFull">完整备份</button><button id="wPull">合并云端增量</button>`)) +
    card("从完整备份恢复", `<button id="wList">列出云端备份</button><div id="wBox"></div>`);
  bindAuto(m);
  $("#wDelta").onclick = () => C.safe(async () => { const r = await backupDialog("webdav_delta", "增量上传"); if (!r.uploaded) C.toast(r.reason || "没有新的变化"); dataWebdav(m); });
  $("#wFull").onclick = () => C.safe(() => backupDialog("webdav", "WebDAV 完整备份"));
  $("#wPull").onclick = () => confirm("把云端的全部增量合并进本机？（按时间新旧合并，不删除本机数据）") && C.safe(async () => {
    const r = await C.api("/api/data/restore", { method: "POST", body: { target: "webdav_delta", confirm: true } }); C.toast(`合并完成：新增 ${r.added}，更新 ${r.updated}`); C.refresh?.(); });
  $("#wList").onclick = () => restoreList(m, "webdav", "#wBox");
}
async function dataCloud(m) {
  const info = await C.safe(() => C.api("/api/data/info")) || { last: {} };
  m.innerHTML = card("云服务器", `<div class="sv-help" style="margin:0 0 6px">把另一个星愿部署在你自己的服务器上（同一个程序，配好主人令牌），这里填它的地址和主人令牌，就能双向同步记忆：只传变化的部分，较新的覆盖较旧的；对方的核心记忆只会变成提案，由你批准。</div>` +
      row("服务器地址", inp("data.cloud_url", "text", "https://my-server:7860", "w-l")) + row("主人令牌", inp("data.cloud_token", "secret", "", "w-m")) +
      row("检测", `<button id="cChk">检测连接</button>`) +
      row("每天 03:00 自动同步", tog("data.cloud_daily"), "错过了下次启动补上。") +
      row("上次推送 / 拉取", `<span class="muted">${fmtT(info.last?.last_cloud_push)} / ${fmtT(info.last?.last_cloud_pull)}</span>`) +
      row("立即同步", `<button class="primary" id="cSync">推送 + 拉取</button><button id="cPull">只拉取</button>`));
  bindAuto(m);
  $("#cChk").onclick = () => C.safe(async () => { const r = await C.api("/api/data/cloud/check", { method: "POST", body: {} }); C.toast("连接正常：星愿 v" + r.version); });
  $("#cSync").onclick = () => C.safe(async () => { const r = await C.api("/api/data/backup", { method: "POST", body: { target: "cloud" } });
    C.toast(`推送 ${r.push.pushed} 颗，拉回新增 ${r.pull.added}、更新 ${r.pull.updated}`); C.refresh?.(); dataCloud(m); });
  $("#cPull").onclick = () => C.safe(async () => { const r = await C.api("/api/data/restore", { method: "POST", body: { target: "cloud", confirm: true } });
    C.toast(`拉回新增 ${r.added}、更新 ${r.updated}`); C.refresh?.(); dataCloud(m); });
}
function dataImport(m) {
  m.innerHTML = card("导入外部应用数据", row("文件或文件夹路径", `<input class="sv-in w-l" id="iPath" placeholder="例如 D:\\档案\\某某">`) +
      row("类型", `<select class="sv-in w-m" id="iKind"><option value="auto">自动识别</option><option value="xingxi">星汐档案</option><option value="text">markdown / txt</option><option value="chatgpt">ChatGPT conversations.json</option></select><button class="primary" id="iGo">导入</button>`) +
      row("或上传文件", `<input type="file" id="iFile"><button id="iUp">上传导入</button>`) + `<pre id="iOut" class="hidden"></pre>`);
  const show = (r) => { const o = $("#iOut"); o.classList.remove("hidden"); o.textContent = JSON.stringify(r, null, 2); };
  $("#iGo").onclick = () => C.safe(async () => { show(await C.api("/api/import/path", { method: "POST", body: { path: $("#iPath").value, kind: $("#iKind").value } })); C.toast("导入完成"); C.refresh?.(); });
  $("#iUp").onclick = () => C.safe(async () => { const f = $("#iFile").files[0]; if (!f) return C.toast("先选文件", true);
    const fd = new FormData(); fd.append("file", f); fd.append("kind", $("#iKind").value); show(await C.api("/api/import/upload", { method: "POST", body: fd })); C.toast("导入完成"); C.refresh?.(); });
}

// ———————————————— 核心记忆 ————————————————
function pageCore(m) {
  m.innerHTML = card("核心记忆", row("编辑核心记忆", `<a class="sv-link" href="/core">核心记忆 ↗</a>`, "AI 是谁、主人是谁、主人的喜好。自动通道（整理、对话监控、同步）只能提提案，不会直接改。") +
    row("代理里识别 /核心 指令", tog("core.chat_commands")) + row("识别「记住，以后叫我X」这类原话", tog("core.natural_commands")) +
    row("MCP 的 update_core 带主人原话时直接生效", tog("core.mcp_owner_edit"), "关掉则全部转为提案。"));
  bindAuto(m);
}

// ———————————————— 关于 ————————————————
async function pageAbout(m) {
  const h = await C.safe(() => C.api("/api/health")) || {};
  m.innerHTML = `<div class="sv-card about"><img src="/static/icons/icon-192.png" alt=""><h2>星愿记忆</h2><div class="muted">StarWish · v${E(h.version || "")}</div>
    <p>可携带的本地 AI 记忆库：记忆库即灵魂，换任何模型都还是同一个它。</p>
    ${row("灵魂包目录", `<code>${E(h.soul_dir || "")}</code>`)}${row("面板地址", `<code>${location.origin}/</code>`)}</div>`;
}
