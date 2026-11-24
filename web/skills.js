// 星愿 StarWish —— 技能进化模块页面
import { saveProtectedSettings } from "./owner-settings.js";

// 事件时间：优先用 Unix 时间戳按本机时区显示，避免显示成 UTC
const evTime = (x) => (x && x.t ? new Date(x.t * 1000).toLocaleString("zh-CN", { hour12: false }) : String((x && x.ts) || "").replace("T", " ").slice(0, 19));
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—");
async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  let d = null; try { d = await r.json(); } catch { d = null; }
  if (!r.ok) {
    const error = new Error((d && (d.detail || d.message)) || `HTTP ${r.status}`);
    error.status = r.status;
    throw error;
  }
  return d;
}
let tt;
function toast(m, err = false) { const t = $("#toast"); t.textContent = m; t.className = err ? "err" : ""; clearTimeout(tt); tt = setTimeout(() => t.classList.add("hidden"), 3800); }
async function safe(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }
const SRC = { owner: "主人", panel: "面板", mcp: "MCP·AI", review: "后台巡检", "review:codex": "Codex 会话巡检",
  api: "API", "api-agent": "API·AI", rollback: "回滚", approve: "批准待审", command: "对话指令" };
const diffHtml = (e) => Object.entries(e.diff || {}).map(([f, d]) => `<div class="diff"><div class="dt">${esc(f)}</div>${d.map(([op, l]) =>
  `<div class="l ${op === "+" ? "add" : op === "-" ? "del" : "eq"}">${op === "=" ? "  " : op + " "}${esc(l) || "&nbsp;"}</div>`).join("")}</div>`).join("");

let ST = null;
async function loadStatus() {
  ST = await api("/api/evolve/status");
  const cr = ST.codex_review || {};
  $("#emeta").innerHTML = `<span class="chip">技能 <b>${ST.total}</b></span><span class="chip">AI 自建 <b>${ST.agent_created}</b></span>
    <span class="chip">通用巡检 <b>${ST.iters_since_skill}/${ST.nudge_interval}</b></span>
    <span class="chip">Codex 待巡检 <b>${Number(cr.pending_turns || 0)} 轮</b></span>
    ${cr.retry ? `<span class="chip">Codex 待重试 <b>${Number(cr.retry)}</b></span>` : ""}
    ${cr.uncertain ? `<span class="chip">Codex 结果待核对 <b>${Number(cr.uncertain)}</b></span>` : ""}
    ${ST.running || cr.running ? '<span class="chip"><b>巡检中…</b></span>' : ""}`;
  const b = $("#pBadge");
  const pending = Number(ST.pending || 0) + Number(cr.uncertain || 0);
  b.textContent = pending; b.classList.toggle("hidden", !pending);
  $("#intro").innerHTML = `技能是 AI 的<b>操作手册</b>（怎么做某件事），和星星（发生过什么）、核心记忆（我是谁）并列。参考 Hermes Agent：
    <b>前台自觉</b>——会用工具的 AI 做完复杂任务、修好难题后自己调用 <code>skill_manage</code> 存成技能，发现过时立即 patch；
    <b>后台巡检</b>——通用巡检累计 ${ST.nudge_interval} 轮；普通 Codex 则在手动授权的工作区内按会话各自计数，达到同一阈值才回看本会话。Codex 候选技能一律待审。
    AI 自建的技能都过安全扫描，危险的交给你批准；每次修改都可追溯、可回滚；不用的技能只会变暗或归档，<b>永不硬删除</b>。`;
}

// ---------------- 技能库 ----------------
let showArch = false, filter = "";
async function renderLib() {
  const list = await safe(() => api("/api/skills?archived=" + showArch));
  if (!list) return;
  const f = filter.trim().toLowerCase();
  const items = list.filter((s) => !f || (s.name + s.description + s.tags.join(" ")).toLowerCase().includes(f));
  $("#tab-lib").innerHTML = `<div class="toolbar"><input id="flt" placeholder="筛选技能…" value="${esc(filter)}">
      <button id="newBtn" class="primary">＋ 新建技能</button><button id="archBtn">${showArch ? "隐藏归档" : "显示归档"}</button></div>
    ${items.length ? "" : '<div class="empty">还没有技能。AI 做完复杂任务后会自己沉淀，也可以点「立即巡检」或手动新建。</div>'}
    <div class="skgrid">${items.map((s) => `<div class="sk ${s.archived ? "arch" : ""}" data-sk="${esc(s.name)}">
      <div class="glow" style="opacity:${Math.min(1, s.brightness / 1.5).toFixed(2)}"></div>
      <h4>${esc(s.name)} <span class="who ${s.created_by === "agent" ? "agent" : "owner"}">${s.created_by === "agent" ? "AI 自建" : "主人"}</span>${s.archived ? '<span class="who">已归档</span>' : ""}</h4>
      <div class="d">${esc(s.description)}</div>
      <div class="bright-bar"><i style="width:${Math.min(100, (s.brightness / 1.5) * 100)}%"></i></div>
      <div class="m"><span>使用 ${s.views + s.injections}</span><span>修补 ${s.patches}</span><span>👍${s.good} 👎${s.bad}</span><span>${s.tokens} tok</span>${s.tags.map((t) => `<span>#${esc(t)}</span>`).join("")}</div>
    </div>`).join("")}</div>`;
  $("#flt").oninput = (e) => { filter = e.target.value; clearTimeout(window._ft); window._ft = setTimeout(() => { renderLib().then(() => { const i = $("#flt"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 250); };
  $("#archBtn").onclick = () => { showArch = !showArch; renderLib(); };
  $("#newBtn").onclick = renderNew;
  $$("[data-sk]").forEach((el) => (el.onclick = () => renderSkill(el.dataset.sk)));
}

function renderNew() {
  const tpl = `---\nname: my-skill\ndescription: 什么时候用 + 做什么（一句话）\ntags: [示例]\n---\n# 标题\n\n## 适用场景\n\n## 步骤\n1. \n\n## 注意 / 踩过的坑\n- 规则 + 原因\n`;
  $("#tab-lib").innerHTML = `<div class="hcard editor"><div class="hhead"><span class="rev">新建技能</span><div class="acts"><button id="cancel">返回</button></div></div>
    <div class="kvs"><span>名字</span><input id="nName" placeholder="小写英文-连字符，如 weekly-report"><span>分类（可选）</span><input id="nCat" placeholder="如 writing"></div>
    <label>SKILL.md</label><textarea id="nContent">${esc(tpl)}</textarea>
    <div class="row"><button class="primary" id="nSave">创建</button></div></div>`;
  $("#cancel").onclick = renderLib;
  $("#nSave").onclick = () => safe(async () => {
    const name = $("#nName").value.trim();
    const content = $("#nContent").value.replace(/^name:.*$/m, "name: " + name);
    const r = await api("/api/skills/manage", { method: "POST", body: { action: "create", name, category: $("#nCat").value.trim() || null, content } });
    toast(r.message); await loadStatus(); renderSkill(name);
  });
}

async function renderSkill(name) {
  const [s, hist] = await Promise.all([api("/api/skills/" + encodeURIComponent(name)), api(`/api/skills/${encodeURIComponent(name)}/history?limit=50`)]);
  $("#tab-lib").innerHTML = `<div class="hcard editor">
    <div class="hhead"><span class="rev">${esc(s.name)}</span><span class="who ${s.created_by === "agent" ? "agent" : "owner"}">${s.created_by === "agent" ? "AI 自建" : "主人"}</span>
      <span class="t">亮度 ${s.brightness.toFixed(2)} · 使用 ${s.views + s.injections} · 修补 ${s.patches} · 最近使用 ${fmtDate(s.last_used)}</span>
      <div class="acts"><button id="back">← 技能库</button></div></div>
    <div class="note">${esc(s.description)}</div>
    ${s.files.length ? `<div class="note">辅助文件：${s.files.map(esc).join("、")}</div>` : ""}
    <textarea id="sContent" ${s.archived ? "disabled" : ""}>${esc(s.content)}</textarea>
    <div class="row">
      ${s.archived ? '<button class="primary" id="sRestore">恢复</button>' : `<button class="primary" id="sSave">保存（整篇）</button>
      <button id="sGood">👍 好用</button><button id="sBad">👎 有问题</button><button class="danger" id="sArch">归档</button>`}
    </div></div>
    <h3 style="color:var(--accent2);font-size:14px;margin-top:20px">版本历史（${hist.length}）</h3>
    ${hist.map((e) => `<div class="hcard"><div class="hhead"><span class="rev">r${e.rev}</span><span class="src s-${esc(e.source)}">${esc(SRC[e.source] || e.source)}</span>
      <span class="src">${esc(e.action)}</span>${e.strategy && e.strategy !== "exact" && e.strategy !== "rewrite" ? `<span class="src">模糊匹配 ${esc(e.strategy)}</span>` : ""}
      <span class="t">${evTime(e)}</span>
      <div class="acts"><button data-to="${e.rev}">回到此版本</button><button class="danger" data-undo="${e.rev}">撤销这次</button></div></div>
      ${e.note ? `<div class="note">说明：${esc(e.note)}</div>` : ""}${diffHtml(e)}</div>`).join("")}`;
  $("#back").onclick = renderLib;
  const act = (body, msg) => safe(async () => { const r = await api("/api/skills/manage", { method: "POST", body: { name, ...body } }); toast(r.message || msg); await loadStatus(); renderSkill(name); });
  if ($("#sSave")) $("#sSave").onclick = () => confirm("保存整篇 SKILL.md？会生成新版本。") && act({ action: "edit", content: $("#sContent").value });
  if ($("#sArch")) $("#sArch").onclick = () => confirm("归档后不再注入，可随时恢复。确认？") && act({ action: "archive" });
  if ($("#sRestore")) $("#sRestore").onclick = () => act({ action: "restore" });
  const fb = (good) => safe(async () => { const c = good ? "" : prompt("哪里有问题？（会交给下次巡检优先修复）") || ""; const r = await api(`/api/skills/${encodeURIComponent(name)}/feedback`, { method: "POST", body: { good, comment: c } }); toast(r.message); renderSkill(name); });
  if ($("#sGood")) $("#sGood").onclick = () => fb(true);
  if ($("#sBad")) $("#sBad").onclick = () => fb(false);
  const rb = (rev, mode) => confirm(mode === "undo" ? `撤销 r${rev}？` : `回到 r${rev}？`) && safe(async () => {
    const r = await api(`/api/skills/${encodeURIComponent(name)}/rollback`, { method: "POST", body: { rev, mode, confirm: true } }); toast(r.message); renderSkill(name); });
  $$("[data-to]").forEach((b) => (b.onclick = () => rb(+b.dataset.to, "to")));
  $$("[data-undo]").forEach((b) => (b.onclick = () => rb(+b.dataset.undo, "undo")));
}

// ---------------- 待批准 ----------------
let pendingOwnerToken = "", pendingRender = 0;
function pendingApi(path, options = {}) {
  return api(path, { ...options, headers: pendingOwnerToken ? { "X-StarWish-Owner": pendingOwnerToken } : {} });
}

function renderPendingLogin(container, rejected) {
  container.innerHTML = `<form class="hcard editor" id="pendingOwnerForm">
    <h3>验证主人身份</h3>
    <p class="note">${rejected ? "令牌不正确或已失效，请重新输入。" : "查看或处理待审技能需要主人权限。"}</p>
    <label for="pendingOwnerInput">主人令牌</label>
    <input id="pendingOwnerInput" type="password" autocomplete="off" required>
    <div class="row"><button class="primary">验证并查看</button></div>
  </form>`;
  const input = container.querySelector("input");
  container.querySelector("form").onsubmit = (event) => {
    event.preventDefault();
    pendingOwnerToken = input.value;
    input.value = "";
    renderPending();
  };
  input.focus();
}

async function decidePending(button, approve) {
  const card = button.closest(".hcard");
  const id = approve ? button.dataset.ok : button.dataset.no;
  if (!approve && card.dataset.pendingStatus === "applying" &&
      !confirm("这只会移出待审；不会撤销可能已写入的技能。确认移出？")) return;
  const buttons = [...card.querySelectorAll("button:not(:disabled)")];
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const action = approve ? "approve" : "reject";
    const result = await pendingApi(`/api/skills/pending/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    if (!result?.success) throw new Error(result?.message || "未能处理待审项");
    toast(approve ? result.message || "已批准" : "已拒绝");
    await safe(loadStatus);
  } catch (error) {
    toast(error.message, true);
  } finally {
    await renderPending();
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function renderPending() {
  const version = ++pendingRender;
  const container = $("#tab-pending");
  let list, recoveries = [];
  try {
    list = await pendingApi("/api/skills/pending");
  } catch (error) {
    if (version !== pendingRender) return;
    if (error.status === 403) {
      const rejected = !!pendingOwnerToken;
      pendingOwnerToken = "";
      renderPendingLogin(container, rejected);
    } else {
      container.textContent = "无法加载待审列表，请重新打开此页重试。";
      toast(error.message, true);
    }
    return;
  }
  let recoveryError = false;
  try {
    recoveries = await pendingApi("/api/evolve/codex-review/recovery");
  } catch {
    recoveryError = true;
  }
  if (version !== pendingRender) return;
  container.innerHTML = `<div class="intro" style="margin-top:12px">AI 自建技能被安全扫描判为危险、AI 想归档你创建的技能、或设置为「全部需批准」时，会停在这里等你决定。</div>
    ${recoveryError ? '<div class="note warn">暂时无法读取待核对的巡检批次。</div>' : ""}
    ${recoveries.map((r, i) => `<div class="hcard">
      <div class="hhead"><span class="rev">巡检结果待核对</span><span class="src">${esc(r.workspace)}</span></div>
      <div class="note">会话 ${esc(r.session_id)} · 仍有 ${Number(r.pending)} 项同批待审。结案不会批准、拒绝或重发建议。</div>
      ${r.legacy ? '<div class="note warn">该旧批次没有轮次边界。结案将跳过此会话当前积压的复盘，之后仅处理新轮次。</div>' : ""}
      <button data-recovery="${i}">已核对，结案并继续巡检</button></div>`).join("")}
    ${list.length ? "" : '<div class="empty">没有待批准的技能修改。</div>'}
    ${list.map((p) => `<div class="hcard" data-pending-status="${esc(p.status || "pending")}"><div class="hhead"><span class="rev">${esc(p.op.action)}</span><span class="src">${esc(p.op.name)}</span>
      <span class="t">${fmtDate(p.created_at)} · ${esc(SRC[p.source] || p.source)}</span>
      <div class="acts"><button class="primary" data-ok="${esc(p.id)}" ${p.status === "applying" ? "disabled" : ""}>${p.status === "failed" ? "重试批准" : "批准"}</button><button class="danger" data-no="${esc(p.id)}">${p.status === "applying" ? "移出待审" : "拒绝"}</button></div></div>
      <div class="note">原因：${esc(p.reason)}</div>
      ${p.status === "failed" ? `<div class="note warn">上次批准未生效：${esc(p.last_error)}</div>` : ""}
      ${p.status === "applying" ? '<div class="note warn">批准结果未确认。请先检查技能及历史，不要重复应用。</div>' : ""}
      ${p.scan && p.scan.findings.length ? `<div class="scan">${p.scan.findings.map((f) => `<div class="f">[${esc(f.severity)}] ${esc(f.category)}：${esc(f.desc)}（${esc(f.file)}:${f.line}）</div>`).join("")}</div>` : ""}
      <pre>${esc(p.op.content || (p.op.old_string ? `- ${p.op.old_string}\n+ ${p.op.new_string}` : p.op.file_content || ""))}</pre></div>`).join("")}`;
  container.querySelectorAll("[data-ok]").forEach((b) => { b.onclick = () => decidePending(b, true); });
  container.querySelectorAll("[data-no]").forEach((b) => { b.onclick = () => decidePending(b, false); });
  container.querySelectorAll("[data-recovery]").forEach((b) => {
    b.onclick = async () => {
      const r = recoveries[Number(b.dataset.recovery)];
      const msg = r.legacy ? "结案将跳过此会话当前积压的复盘，且不会删除已有待审建议。继续？"
        : "请先检查待审和技能变更记录。结案不会重放本批次，后续轮次会继续复盘。继续？";
      if (!confirm(msg)) return;
      b.disabled = true;
      try {
        await pendingApi(`/api/evolve/codex-review/recovery/${encodeURIComponent(r.batch)}/acknowledge`,
          { method: "POST", body: { through: r.through } });
        toast("巡检批次已结案");
        await safe(loadStatus);
      } catch (error) {
        toast(error.message, true);
      } finally {
        await renderPending();
      }
    };
  });
}

// ---------------- 进化日志 ----------------
async function renderLog() {
  const [st, hist] = await Promise.all([api("/api/evolve/status"), api("/api/skills/history?limit=60")]);
  const lr = st.last_review;
  $("#tab-log").innerHTML = `<div class="hcard"><div class="hhead"><span class="rev">上次巡检</span><span class="t">${lr ? fmtDate(lr.t) : "还没有巡检过"}</span></div>
    ${lr ? (lr.ok ? `<div class="note">${esc(lr.reason || "")} · 回看 ${lr.turns ?? 0} 轮 · 生效 ${lr.applied ?? 0} · 待批准 ${lr.staged ?? 0} · 新星 ${lr.stars_added ?? 0} · 核心提案 ${lr.core_proposals ?? 0}</div>
      <div class="note">${esc(lr.summary || "")}</div>${(lr.skills || []).map((x) => `<div class="note">${x.success ? "✔" : "✘"} ${esc(x.op)} ${esc(x.name)}：${esc(x.message || "")}</div>`).join("")}`
      : `<div class="note warn">失败：${esc(lr.error || lr.reason || "")}</div>`) : ""}
    ${Object.keys(st.needs_fix || {}).length ? `<div class="note">待修复（被差评）：${Object.entries(st.needs_fix).map(([k, v]) => esc(k) + "（" + esc(v) + "）").join("、")}</div>` : ""}</div>
    <h3 style="color:var(--accent2);font-size:14px;margin-top:18px">全部技能修改</h3>
    ${hist.map((e) => `<div class="hcard"><div class="hhead"><span class="rev">${esc(e.name)} r${e.rev}</span><span class="src s-${esc(e.source)}">${esc(SRC[e.source] || e.source)}</span>
      <span class="src">${esc(e.action)}</span><span class="t">${evTime(e)}</span></div>
      ${e.note ? `<div class="note">${esc(e.note)}</div>` : ""}${diffHtml(e)}</div>`).join("") || '<div class="empty">暂无</div>'}`;
}

// ---------------- 设置 ----------------
async function renderSettings() {
  const cfg = await api("/api/settings"); const e = cfg.evolve || {};
  $("#tab-settings").innerHTML = `<div class="hcard"><div class="kvs">
    <span>自进化</span><label><input type="checkbox" id="e_en" ${e.enabled !== false ? "checked" : ""}> 开启（关闭后 AI 不能改技能，也不巡检）</label>
    <span>巡检阈值</span><input id="e_n" type="number" min="0" value="${e.nudge_interval ?? 10}" title="累计多少轮对话没有新技能时后台巡检；0=不自动巡检">
    <span>审批方式</span><select id="e_ap"><option value="auto" ${e.approval !== "always" ? "selected" : ""}>自动：安全的直接生效，危险的交我批准</option><option value="always" ${e.approval === "always" ? "selected" : ""}>AI 的所有技能修改都要我批准</option></select>
    <span>每次巡检最多改</span><input id="e_ops" type="number" min="1" max="20" value="${e.max_ops ?? 5}">
    <span>联合巡检</span><label><input type="checkbox" id="e_mem" ${e.review_memory !== false ? "checked" : ""}> 顺便提取星星 / 提出核心提案（核心只提案）</label>
    <span>代理注入技能</span><label><input type="checkbox" id="e_inj" ${e.inject !== false ? "checked" : ""}> 注入相关技能</label>
    <span>注入预算</span><input id="e_b" type="number" min="50" value="${e.inject_budget_tokens ?? 400}">
  </div><div class="row"><button class="primary" id="e_save">保存</button></div></div>`;
  $("#e_save").onclick = () => safe(async () => {
    await saveProtectedSettings(api, { evolve: { enabled: $("#e_en").checked, nudge_interval: +$("#e_n").value, approval: $("#e_ap").value,
      max_ops: +$("#e_ops").value, review_memory: $("#e_mem").checked, inject: $("#e_inj").checked, inject_budget_tokens: +$("#e_b").value } });
    toast("已保存"); loadStatus();
  });
}

$("#reviewBtn").onclick = () => safe(async () => {
  const r = await api("/api/evolve/review", { method: "POST", body: { background: true } });
  toast(r.ok ? (r.started ? "已在后台开始巡检" : "巡检正在进行中") : r.reason, !r.ok);
  setTimeout(() => { loadStatus(); if (tab === "log") renderLog(); }, 2500);
});

let tab = "lib";
function refreshTab() { ({ lib: renderLib, pending: renderPending, log: renderLog, settings: renderSettings })[tab](); }
$$(".ctabs button").forEach((b) => (b.onclick = () => {
  tab = b.dataset.tab;
  $$(".ctabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((x) => x.classList.toggle("hidden", x.id !== "tab-" + tab));
  history.replaceState(null, "", "#" + tab); refreshTab();
}));
(async () => {
  await safe(loadStatus);
  const h = location.hash.slice(1);
  if (["pending", "log", "settings"].includes(h)) $(`.ctabs [data-tab="${h}"]`).click(); else renderLib();
})();

// 桌面版心跳：页面全部关闭后，后台服务才会自动退出（仅浏览器兜底模式用到）
setInterval(() => fetch("/api/ping", { method: "POST" }).catch(() => {}), 15000); fetch("/api/ping", { method: "POST" }).catch(() => {});
