// 星愿记忆 —— 设置窗口（v0.8.0）：独立浮动窗口，点「设置」开 / 关；可拖动、可调大小、Esc 关闭。
// 模型服务仿照常见桌面客户端：服务商列表 → 填 Key → 一键同步模型 → 每个模型都能「检测」。
let C = null;            // 由 app.js 传入：{ api, toast, esc, safe, refresh, embedPanel }
let cfg = null;          // /api/settings（掩码后的配置）
let presets = [];
let page = "providers";
let selId = "";
let provFilter = "";
let modelFilter = "";
let saveTimer = null;

const PAGES = [
  ["providers", "☁", "模型服务"], ["defaults", "◎", "默认模型"], ["embed", "✶", "向量模型"], ["video", "👁", "视频交互模型"],
  ["access", "⇄", "接入与代理"], ["memory", "✦", "记忆与星辰"], ["backup", "⇪", "备份与同步"], ["security", "🔒", "安全"], ["about", "ⓘ", "关于"],
];
const KIND = { chat: "对话", vision: "识图", embedding: "向量", other: "其他" };
const ROLES = [
  ["chat", "对话模型", "代理转发给聊天客户端时默认用它（客户端自己指定模型时以客户端为准）", ["chat", "vision"]],
  ["maintain", "整理模型", "后台整理记忆、融合归纳、技能巡检用；留空 = 和对话模型相同。建议选便宜快速的", ["chat", "vision"]],
  ["vision", "识图模型", "宇宙之眼「通用帧模式」看屏幕 / 摄像头时用，必须支持图片输入", ["vision", "chat"]],
  ["embed", "向量模型（接口档）", "向量模型选「③ 接口」时用；内置 bge-small 不需要它", ["embedding"]],
];

const $ = (s) => document.querySelector(s);
const E = (s) => C.esc(s);
const uid = (p) => `${p}-${Math.random().toString(36).slice(2, 6)}`;

export function initSettings(ctx) {
  C = ctx;
  const win = document.createElement("div");
  win.id = "setWin";
  win.className = "setwin hidden";
  win.innerHTML = `
    <div class="sw-head" id="swHead"><img src="/static/icons/icon-32.png" alt=""><b>设置</b><span class="sw-tip" id="swTip"></span>
      <button class="sw-x" id="swClose" title="关闭（Esc）">×</button></div>
    <div class="sw-body"><nav class="sw-nav" id="swNav"></nav><section class="sw-main" id="swMain"></section></div>`;
  document.body.appendChild(win);
  $("#swClose").onclick = closeSettings;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !win.classList.contains("hidden")) closeSettings(); });
  drag(win, $("#swHead"));
}

export function settingsOpen() { return !$("#setWin").classList.contains("hidden"); }
export function closeSettings() { $("#setWin").classList.add("hidden"); document.querySelector('[data-panel="settings"]')?.classList.remove("on"); }
export async function toggleSettings(force) {
  const open = force ?? !settingsOpen();
  if (!open) return closeSettings();
  $("#setWin").classList.remove("hidden");
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
  $("#swNav").innerHTML = PAGES.map(([k, i, t]) => `<button data-p="${k}" class="${k === page ? "on" : ""}"><span>${i}</span>${t}</button>`).join("");
  $("#swNav").querySelectorAll("button").forEach((b) => (b.onclick = () => { page = b.dataset.p; renderNav(); render(); }));
}

function render() {
  const m = $("#swMain");
  m.className = "sw-main" + (page === "providers" ? " three" : "");
  ({ providers: pageProviders, defaults: pageDefaults, embed: pageEmbed, video: pageVideo, access: pageAccess,
     memory: pageMemory, backup: pageBackup, security: pageSecurity, about: pageAbout })[page](m);
}

// ———————————————— 保存 ————————————————
function queueSave(delay = 500) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => C.safe(saveProviders), delay);
}
async function saveProviders() {
  clearTimeout(saveTimer);
  const providers = cfg.providers.map(({ api_key_set, ...p }) => p);
  const r = await C.api("/api/settings", { method: "PUT", body: { providers, defaults: cfg.defaults } });
  // 保持本地对象不变（界面里正拿着它们），只换成服务端的掩码 Key 与映射后的其他字段
  const byId = new Map(r.providers.map((x) => [x.id, x]));
  cfg.providers.forEach((x) => { const y = byId.get(x.id); if (y) { x.api_key = y.api_key; x.api_key_set = y.api_key_set; } });
  const { providers: _p, defaults: _d, ...rest } = r; Object.assign(cfg, rest);
  flash("已自动保存");
}
function flash(t) { const el = $("#swTip"); if (!el) return; el.textContent = t; el.classList.add("pulse"); setTimeout(() => el.classList.remove("pulse"), 900); }

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
  m.innerHTML = `<h2>默认模型</h2><div class="sub">从已同步的模型里选，不用手打模型名。下拉里没有想要的？先到「模型服务」同步或手动添加。</div>
    ${ROLES.map(([r, t, desc, kinds]) => `<div class="def-row"><div><b>${t}</b><div class="muted">${desc}</div></div>
      <div class="f-row"><select data-role="${r}">${opts(kinds, cfg.defaults[r] || "")}</select><button data-chk="${r}">检测</button></div>
      <div class="mres" id="dr-${r}"></div></div>`).join("")}
    ${cfg.providers.length ? "" : '<div class="sw-empty">还没有服务商：先到「模型服务」添加一家并填 Key。</div>'}`;
  m.querySelectorAll("select[data-role]").forEach((s) => (s.onchange = () => { cfg.defaults[s.dataset.role] = s.value; C.safe(saveProviders).then(() => C.refresh?.()); }));
  m.querySelectorAll("[data-chk]").forEach((b) => (b.onclick = async () => {
    const r = b.dataset.chk, out = $("#dr-" + r); out.className = "mres"; out.textContent = "检测中…"; b.disabled = true;
    try { if (saveTimer) await saveProviders(); const x = await C.api("/api/providers/check", { method: "POST", body: { role: r } });
      out.className = "mres ok"; out.textContent = `✓ ${x.msg}${x.reply ? " · 「" + x.reply + "」" : ""}`;
    } catch (e) { out.className = "mres bad"; out.textContent = "✗ " + e.message; }
    b.disabled = false;
  }));
}

// ———————————————— 通用表单页 ————————————————
function field(path, label, type = "text", ph = "", hint = "") {
  const [a, b] = path.split("."); const v = cfg[a]?.[b];
  if (type === "checkbox") return `<label class="chk"><input type="checkbox" data-k="${path}" ${v ? "checked" : ""}> ${label}</label>`;
  if (type === "secret") return `<label>${label}</label><input data-k="${path}" type="password" value="" placeholder="${cfg[a]?.[b + "_set"] ? "已设置（留空则不修改）" : "未设置"}">`;
  return `<label>${label}</label><input data-k="${path}" type="${type}" value="${E(v)}" placeholder="${E(ph)}">${hint ? `<div class="f-hint">${hint}</div>` : ""}`;
}
function choose(path, label, opts) {
  const [a, b] = path.split(".");
  return `<label>${label}</label><select data-k="${path}">${opts.map(([v, t]) => `<option value="${E(v)}" ${String(cfg[a]?.[b]) === String(v) ? "selected" : ""}>${E(t)}</option>`).join("")}</select>`;
}
function form(m, title, sub, body, after) {
  m.innerHTML = `<h2>${title}</h2>${sub ? `<div class="sub">${sub}</div>` : ""}${body}<div class="sw-save"><button class="primary" id="fSave">保存</button></div>`;
  $("#fSave").onclick = () => C.safe(async () => {
    const patch = {};
    m.querySelectorAll("[data-k]").forEach((el) => {
      const [a, b] = el.dataset.k.split(".");
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "number") v = v === "" ? 0 : Number(v);
      if (el.type === "password" && v === "") return;
      (patch[a] = patch[a] || {})[b] = v;
    });
    cfg = await C.api("/api/settings", { method: "PUT", body: patch });
    C.toast("设置已保存"); C.refresh?.(); render();
  });
  after && after();
}

function pageEmbed(m) {
  form(m, "向量模型（语义检索）", "和 jieba + FTS5 一起找记忆：字面没对上也能按意思找到（搜「保暖外套」能找到「羽绒服」）。选的档位没准备好时自动用内置模型。",
    `${field("memory.semantic", "启用语义检索", "checkbox")}
    ${choose("embed.provider", "使用哪个向量模型", [["small", "① 内置 bge-small-zh int8（23.9MB · 已随软件附带）"], ["m3", "② BGE-M3 int8（约 590MB · 一键部署）"], ["api", "③ 接口（在「默认模型」里选向量模型）"]])}
    <div id="embBox" class="sub">正在读取向量模型状态…</div>
    <details class="more"><summary>下载与高级</summary>
      <div class="grid2"><div>${field("embed.hf_mirror", "模型下载镜像", "text", "https://hf-mirror.com")}</div><div>${field("embed.api_dimensions", "③ 向量维度（0 = 服务默认）", "number")}</div></div>
      ${field("embed.prefer_mirror", "先走镜像（国内网络建议勾选）", "checkbox")}</details>`, () => C.embedPanel());
}

function pageVideo(m) {
  form(m, "视频交互模型（宇宙之眼）", "陪你看屏幕 / 摄像头时用。通用帧模式用「默认模型 → 识图模型」；千问视频接口单独填 Key。",
    `${choose("eye.engine", "引擎", [["frame", "通用帧模式（任意识图模型）"], ["qwen_omni", "千问全模态视频接口（Qwen3.8-Omni-Flash）"]])}
    <div class="card">通用帧模式当前识图模型：<b>${E(cfg.eye.frame_model || "未选择（沿用对话模型）")}</b> · <a href="#" id="goDef">去「默认模型」选择</a></div>
    ${choose("eye.frame_compose", "多帧怎么发", [["grid", "拼成一张分镜图（省钱，单图模型也能用）"], ["multi", "每帧一张图"]])}
    <h3>千问视频接口</h3>
    <div class="grid2"><div>${choose("eye.qwen_protocol", "协议", [["openai", "OpenAI 兼容"], ["anthropic", "Anthropic 兼容"]])}</div><div>${field("eye.qwen_model", "模型")}</div>
    <div>${field("eye.qwen_api_key", "千问 API Key", "secret")}</div><div>${choose("eye.qwen_video_mode", "帧序列传法", [["video", "按视频传（失败自动退回多图）"], ["images", "每帧一张图"]])}</div></div>`,
    () => { $("#goDef").onclick = (e) => { e.preventDefault(); page = "defaults"; renderNav(); render(); }; });
}

function pageAccess(m) {
  const o = location.origin;
  form(m, "接入与代理", "聊天客户端把 API 地址改成下面的「OpenAI 兼容」地址，记忆就会自动注入。",
    `<div class="kv"><span>OpenAI 兼容</span><code>${o}/v1</code><span>REST API</span><code>${o}/api</code><span>MCP</span><code id="mcpCmd">MCP接入.bat（或 python mcp_server.py）</code></div>
    ${field("proxy.inject", "自动注入核心记忆 + 相关星星", "checkbox")}
    ${field("proxy.log_to_archive", "把对话写入归档，供日后整理", "checkbox")}
    <div class="grid2"><div>${field("proxy.budget_tokens", "每轮召回预算（tokens）", "number")}</div><div>${field("proxy.model_override", "强制模型（可空）")}</div></div>
    ${field("proxy.proxy_key", "代理密钥（可空；填了则客户端需用它作为 API Key）", "secret")}
    ${field("core.owner_token", "主人令牌（远程直接改核心记忆用，可空）", "secret")}`,
    () => {
      fetch("/api/health").then((r) => r.json()).then((h) => {
        const el = $("#mcpCmd");
        if (el && h.exe) el.textContent = `"${h.exe}" --mcp`;
      }).catch(() => {});
    });
}

function pageMemory(m) {
  form(m, "记忆与星辰", "越新的星越亮，老星只是变暗、不会消失；相似的星会融合进化。",
    `<div class="grid2"><div>${field("memory.half_life_days", "亮度半衰期（天）", "number")}</div><div>${field("memory.brightness_floor", "最低亮度", "number")}</div>
    <div>${field("memory.brightness_weight", "亮度在排序中的权重", "number")}</div><div>${field("memory.recall_budget_default", "默认召回预算", "number")}</div></div>
    <h3>星辰融合</h3>${field("memory.fuse_auto", "自动融合相似的记忆", "checkbox")}
    <div class="grid2"><div>${field("memory.fuse_every_new", "每新增几颗星检查一次", "number")}</div><div>${field("memory.fuse_threshold", "相似度门槛", "number")}</div></div>`);
}

function pageBackup(m) {
  form(m, "备份与同步", "灵魂全部在 soul/ 文件夹。WebDAV 备份会先加密再上传。",
    `${field("webdav.url", "WebDAV 地址", "text", "https://dav.jianguoyun.com/dav/")}
    <div class="grid2"><div>${field("webdav.user", "账号")}</div><div>${field("webdav.password", "密码 / 应用密码", "secret")}</div>
    <div>${field("webdav.remote_dir", "远端目录")}</div><div>${field("webdav.keep_versions", "保留版本数", "number")}</div></div>
    ${field("webdav.passphrase", "备份口令（加密上传的快照，请牢记）", "secret")}
    <h3>定时任务（小时，0 = 关闭）</h3>
    <div class="grid2"><div>${field("schedules.consolidate_hours", "LLM 整理", "number")}</div><div>${field("schedules.backup_hours", "WebDAV 备份", "number")}</div>
    <div>${field("schedules.snapshot_hours", "本地快照", "number")}</div><div>${field("schedules.keep_local_snapshots", "本地快照保留数", "number")}</div></div>`);
}

function pageSecurity(m) {
  m.innerHTML = `<h2>安全</h2><div class="sub">用主密码加密 API Key、WebDAV 密码、备份口令。启用后每次启动需在这里解锁（或设置环境变量 STARWISH_MASTER）。</div>
    <div class="card">${cfg.secrets_encrypted ? (cfg.secrets_unlocked ? "已加密，当前已解锁。" : '<span class="warn">已加密，尚未解锁（API Key 等暂不可用）。</span>') : '<span class="warn">目前以明文保存在 soul/config.json。</span>'}</div>
    <input id="master" type="password" placeholder="主密码">
    <div class="row">${cfg.secrets_encrypted ? '<button id="mUnlock" class="primary">解锁</button><button class="danger" id="mOff">关闭加密</button>' : '<button id="mOn" class="primary">启用加密</button>'}</div>`;
  const go = (path, body, msg) => C.safe(async () => { await C.api(path, { method: "POST", body }); C.toast(msg); reload(); });
  if ($("#mOn")) $("#mOn").onclick = () => go("/api/secrets/encrypt", { passphrase: $("#master").value }, "已启用加密");
  if ($("#mUnlock")) $("#mUnlock").onclick = () => go("/api/secrets/unlock", { passphrase: $("#master").value }, "已解锁");
  if ($("#mOff")) $("#mOff").onclick = () => confirm("关闭加密后机密将以明文保存，确认？") && go("/api/secrets/decrypt", {}, "已关闭加密");
}

async function pageAbout(m) {
  const h = await C.safe(() => C.api("/api/health")) || {};
  m.innerHTML = `<div class="about"><img src="/static/icons/icon-192.png" alt=""><h2>星愿记忆</h2><div class="muted">StarWish · v${E(h.version || "")}</div>
    <p>可携带的本地 AI 记忆库：记忆库即灵魂，换任何模型都还是同一个它。</p>
    <div class="kv"><span>灵魂包目录</span><code>${E(h.soul_dir || "")}</code><span>面板地址</span><code>${location.origin}/</code></div></div>`;
}

// ———————————————— 拖动 ————————————————
function drag(win, handle) {
  let sx, sy, ox, oy, on = false;
  handle.addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) return;
    const r = win.getBoundingClientRect(); on = true; sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
    win.style.transform = "none"; win.style.left = ox + "px"; win.style.top = oy + "px"; e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => { if (!on) return;
    win.style.left = Math.max(-win.offsetWidth + 120, Math.min(innerWidth - 120, ox + e.clientX - sx)) + "px";
    win.style.top = Math.max(0, Math.min(innerHeight - 40, oy + e.clientY - sy)) + "px"; });
  window.addEventListener("mouseup", () => (on = false));
}
