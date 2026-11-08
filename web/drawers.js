// 星愿记忆 —— 主页的两个抽屉（v0.11.0）
//  · 对话抽屉：浮动面板，可拖动、可调大小、可收起，记住位置；主人用自然语言指挥 AI 管理星愿（/api/agent/chat，有上下文）。
//  · 任务抽屉：默认在左侧，列出 AI 的任务——星愿自己的（归纳、整理、对话指令、备份、压缩…）和从各客户端捕获的任务；
//    颜色：完成绿、进行中 / 未闭环橙、失败红。每 4 秒刷新一次。
let C = null;
const $ = (s) => document.querySelector(s);
const POS_KEY = "starwish.chatpos";
const TASK_KEY = "starwish.taskdrawer";
const ST = { done: ["st-done", "完成"], closed: ["st-done", "已闭环"], running: ["st-running", "进行中"], open: ["st-running", "未闭环"], failed: ["st-failed", "失败"] };
const CLIENT = { claude: "Claude Code", qoder: "Qoder", workbuddy: "WorkBuddy", codebuddy: "CodeBuddy", codex: "Codex", opencode: "OpenCode", dsh: "DeepSeek Harness" };
let tab = "jobs", openTask = null, sending = false;
let ownerToken = "", chatStatus = null, statusRequest = 0;

const ago = (t) => { if (!t) return ""; const s = Date.now() / 1000 - t; if (s < 60) return "刚刚"; if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前"; return new Date(t * 1000).toLocaleDateString("zh-CN"); };

export function initDrawers(ctx) {
  C = ctx;
  buildChat();
  buildTasks();
  window.addEventListener("starwish-ui", () => { placeChat(false); placeTasks(); });
  window.addEventListener("starwish-agent-config", refreshChatStatus);
}

// ---------------- 对话抽屉 ----------------
function buildChat() {
  const d = document.createElement("div");
  d.id = "chatDrawer";
  d.className = "fdrawer";
  d.innerHTML = `<div class="fd-head" id="chatHead"><span class="fd-dot" aria-hidden="true"></span><b>星愿助手</b>
      <span class="fd-ctx" id="chatCtx" title="上下文用量 / 自动压缩阈值"></span>
      <button class="fd-btn" id="chatClear" title="清空对话历史" disabled>清空</button><button class="fd-btn" id="chatMin" title="收起 / 展开">—</button>
      <div class="fd-meta"><div class="fd-model"><span>模型</span><strong id="chatModel" aria-live="polite">读取中…</strong></div>
        <select class="fd-sel" id="chatEffort" aria-label="思考等级" disabled><option>思考：读取中…</option></select></div>
    </div>
    <div class="fd-body"><div class="chat-log" id="chatLog"></div>
      <form class="chat-form" id="chatForm"><textarea id="chatIn" rows="2" placeholder="让 AI 管理星愿，如「把足球的星归到体育」「整理星系」（Enter 发送）" disabled></textarea>
      <button class="primary" id="chatSend" disabled>发送</button></form></div>
    <div class="fd-resize" id="chatResize" title="拖动调整大小"></div>`;
  document.body.appendChild(d);
  placeChat(true);
  drag(d, $("#chatHead"));
  resize(d, $("#chatResize"));
  $("#chatMin").onclick = () => { const p = pos(); p.collapsed = !p.collapsed; savePos(p); placeChat(false); };
  $("#chatClear").onclick = () => confirm("清空和星愿助手的对话历史？") && C.safe(async () => { await agentApi("/api/agent/history", { method: "DELETE" }); await loadHistory(); });
  $("#chatEffort").onchange = async (e) => {
    const select = e.target, effort = select.value;
    select.disabled = true;
    await C.safe(async () => {
      const r = await C.api("/api/settings", { method: "PUT", body: { llm: { agent_effort: effort } } });
      chatStatus.effort = r.llm.agent_effort;
      C.toast?.("已保存思考等级");
    });
    if (chatStatus) ctxLine(chatStatus);
  };
  $("#chatForm").onsubmit = (e) => { e.preventDefault(); send(); };
  $("#chatIn").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } };
  refreshChatStatus();
}
function pos() { try { return JSON.parse(localStorage.getItem(POS_KEY) || "{}") || {}; } catch { return {}; } }
function savePos(p) { localStorage.setItem(POS_KEY, JSON.stringify(p)); }
function placeChat(first) {
  const d = $("#chatDrawer"), side = document.documentElement.dataset.chatSide || "right";
  let p = pos();
  if (p.side !== side) { p = { side, collapsed: p.collapsed }; savePos(p); }   // 换了默认位置：回到那一侧
  const w = Math.min(p.w || 380, innerWidth - 20), h = Math.min(p.h || 460, innerHeight - 120);
  const x = p.x ?? (side === "left" ? 16 : innerWidth - w - 16);
  const y = p.y ?? Math.max(70, innerHeight - h - 70);
  d.style.setProperty("--chat-top", Math.min(innerHeight - 44, y) + "px");
  Object.assign(d.style, { left: Math.max(0, Math.min(innerWidth - 120, x)) + "px", width: w + "px",
    height: p.collapsed ? "auto" : h + "px" });
  d.classList.toggle("collapsed", !!p.collapsed);
  $("#chatMin").textContent = p.collapsed ? "▢" : "—";
}
function drag(el, handle) {
  let sx, sy, ox, oy, on = false;
  handle.addEventListener("mousedown", (e) => { if (e.target.closest("button,select,input")) return; on = true; sx = e.clientX; sy = e.clientY; ox = el.offsetLeft; oy = el.offsetTop; e.preventDefault(); });
  window.addEventListener("mousemove", (e) => { if (!on) return;
    el.style.left = Math.max(-el.offsetWidth + 120, Math.min(innerWidth - 120, ox + e.clientX - sx)) + "px";
    el.style.setProperty("--chat-top", Math.min(innerHeight - 44, oy + e.clientY - sy) + "px"); });
  window.addEventListener("mouseup", () => { if (!on) return; on = false; const p = pos(); p.x = el.offsetLeft; p.y = el.offsetTop; savePos(p); });
}
function resize(el, grip) {
  let sx, sy, ow, oh, on = false;
  grip.addEventListener("mousedown", (e) => { on = true; sx = e.clientX; sy = e.clientY; ow = el.offsetWidth; oh = el.offsetHeight; e.preventDefault(); e.stopPropagation(); });
  window.addEventListener("mousemove", (e) => { if (!on) return; el.style.width = Math.max(300, ow + e.clientX - sx) + "px"; el.style.height = Math.max(240, oh + e.clientY - sy) + "px"; });
  window.addEventListener("mouseup", () => { if (!on) return; on = false; const p = pos(); p.w = el.offsetWidth; p.h = el.offsetHeight; savePos(p); });
}
function bubble(role, text, actions) {
  const div = document.createElement("div");
  div.className = "msg " + (role === "user" ? "me" : role === "system" ? "sys" : "ai");
  div.innerHTML = C.esc(text).replace(/\n/g, "<br>") + (actions && actions.length ? `<div class="acts">${actions.map((a) => `<span class="act ${a.ok === false ? "bad" : ""}">⚙ ${C.esc(a.tool)}</span>`).join("")}</div>` : "");
  $("#chatLog").appendChild(div);
  $("#chatLog").scrollTop = 1e9;
  return div;
}
function agentApi(path, options = {}, token = ownerToken) {
  return C.api(path, { ...options, headers: token ? { "X-StarWish-Owner": token } : {} });
}

function renderHistory(r) {
  $("#chatLog").innerHTML = "";
  if (!r.history.length) bubble("ai", "你好，我是星愿助手。可以让我搜索、增删改记忆、改核心记忆、整理星系、立即归纳各客户端的新对话、开关某个客户端的监控。");
  r.history.forEach((m) => bubble(m.role, m.role === "system" ? "（较早的对话已自动压缩成摘要）" : m.content));
  ctxLine(r);
}

async function loadHistory() {
  try {
    renderHistory(await agentApi("/api/agent/history"));
  } catch (err) {
    if (err.status === 403) { ownerToken = ""; await refreshChatStatus(); }
    else C.toast?.("无法读取助手对话：" + err.message, true);
  }
}

function showOwnerPrompt(r) {
  const log = $("#chatLog");
  if (log.querySelector("#chatAccess")?.dataset.token === String(r.owner_token_required)) return;
  log.innerHTML = r.owner_token_required
    ? `<form class="chat-access" id="chatAccess" data-token="true">
        <b>验证主人身份后即可聊天</b><div class="f-row">
          <input type="password" id="chatOwnerToken" autocomplete="off" aria-label="主人令牌" placeholder="主人令牌（不是 API Key）" required>
          <button type="submit" class="primary">验证</button>
        </div><div class="muted">令牌只在当前页面使用，刷新后需重新验证。</div>
        <div class="msg bad" role="status"></div>
      </form>`
    : '<div class="msg sys" id="chatAccess" data-token="false">此处仅允许本机访问。请在本机打开星愿助手。</div>';
  const form = log.querySelector("form");
  if (!form) return;
  form.onsubmit = async (e) => {
    e.preventDefault();
    const token = form.querySelector("input").value.trim(), button = form.querySelector("button");
    if (!token) return;
    button.disabled = true;
    try {
      const history = await agentApi("/api/agent/history", {}, token);
      ownerToken = token;
      statusRequest++;
      renderHistory(history);
    } catch (err) {
      form.querySelector('[role="status"]').textContent = err.status === 403 ? "主人令牌不正确。" : err.message;
    } finally {
      button.disabled = false;
    }
  };
}

async function refreshChatStatus() {
  const request = ++statusRequest;
  try {
    const r = await agentApi("/api/agent/status");
    if (request !== statusRequest) return;
    const needsHistory = r.authorized && !chatStatus?.authorized;
    ctxLine(r);
    if (needsHistory) await loadHistory();
    else if (!r.authorized) { ownerToken = ""; showOwnerPrompt(r); }
  } catch (err) {
    if (request !== statusRequest || chatStatus) return;
    $("#chatModel").textContent = "读取失败";
    $("#chatEffort").options[0].textContent = "思考：读取失败";
    $("#chatLog").innerHTML = '<button class="fd-btn" id="chatRetry">重试读取助手状态</button>';
    $("#chatRetry").onclick = refreshChatStatus;
  }
}

function ctxLine(r) {
  if (r.tokens !== undefined || !r.authorized || !chatStatus?.authorized) {
    const used = r.tokens === undefined ? "—" : Math.round(r.tokens / 1000) + "k";
    $("#chatCtx").textContent = `${used} / ${Math.round(r.compact_at / 1000)}k`;
  }
  chatStatus = r;
  const model = $("#chatModel");
  model.textContent = r.model || "未配置对话模型";
  model.title = r.model && !r.model_ready ? r.model + "（凭据不可用）" : model.textContent;
  model.classList.toggle("warn", !!r.model && !r.model_ready);
  $("#chatClear").disabled = $("#chatIn").disabled = !r.authorized;
  $("#chatSend").disabled = !r.authorized || sending;
  const s = $("#chatEffort");
  if (r.efforts && s) {
    s.innerHTML = r.efforts.map(([k, t]) => `<option value="${C.esc(k)}" ${k === r.effort ? "selected" : ""}>思考：${C.esc(t.replace(/（.*）/, ""))}</option>`).join("");
    s.disabled = false;
    s.title = "思考等级：" + (r.efforts.find(([k]) => k === r.effort) || [, ""])[1] + "。也可以发「/思考 高」切换";
  }
}
async function send() {
  const t = $("#chatIn").value.trim();
  if (!t || sending || !chatStatus?.authorized) return;
  sending = true; $("#chatSend").disabled = true;
  $("#chatIn").value = "";
  bubble("user", t);
  const wait = bubble("ai", "思考中…");
  try {
    const r = await agentApi("/api/agent/chat", { method: "POST", body: { text: t } });
    wait.remove();
    if (r.compacted) bubble("system", `上下文到了 ${Math.round(r.compacted.threshold / 1000)}k，较早的对话已自动压缩成摘要。`);
    bubble("ai", r.reply, r.actions);
    if (r.effort_note) bubble("system", r.effort_note);
    if (r.actions && r.actions.length) C.refresh?.();
    C.safe(async () => ctxLine(await agentApi("/api/agent/history")));
    refreshTasks();
  } catch (e) {
    if (e.status === 403) { ownerToken = ""; $("#chatIn").value = t; await refreshChatStatus(); }
    else { wait.textContent = "出错了：" + e.message; wait.classList.add("bad"); }
  }
  sending = false; $("#chatSend").disabled = !chatStatus?.authorized;
}

// ---------------- 任务抽屉 ----------------
function buildTasks() {
  const d = document.createElement("aside");
  d.id = "taskDrawer";
  d.className = "taskdrawer";
  d.innerHTML = `<div class="td-head"><b>任务</b><div class="td-tabs"><button data-t="jobs" class="on">星愿</button><button data-t="client">客户端任务 <span id="tdOpen" class="td-n"></span></button></div>
      <button class="fd-btn" id="tdMin" title="收起">‹</button></div>
    <div class="td-body" id="tdBody"></div>`;
  document.body.appendChild(d);
  const tabBtn = document.createElement("button");
  tabBtn.id = "taskTab"; tabBtn.className = "task-tab hidden"; tabBtn.title = "展开任务列表";
  tabBtn.innerHTML = '任务 <span id="tdBadge" class="td-n"></span>';
  document.body.appendChild(tabBtn);
  d.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { tab = b.dataset.t; d.querySelectorAll("[data-t]").forEach((x) => x.classList.toggle("on", x === b)); refreshTasks(); }));
  $("#tdMin").onclick = () => { localStorage.setItem(TASK_KEY, "closed"); placeTasks(); };
  tabBtn.onclick = () => { localStorage.setItem(TASK_KEY, "open"); placeTasks(); };
  placeTasks();
  refreshTasks();
  setInterval(() => { if (document.visibilityState === "visible") refreshTasks(); }, 4000);
}
function placeTasks() {
  const side = document.documentElement.dataset.taskSide || "left";
  const closed = localStorage.getItem(TASK_KEY) === "closed";
  const d = $("#taskDrawer"), t = $("#taskTab");
  d.classList.toggle("right", side === "right"); t.classList.toggle("right", side === "right");
  d.classList.toggle("hidden", closed); t.classList.toggle("hidden", !closed);
  $("#tdMin").textContent = side === "right" ? "›" : "‹";
  document.body.dataset.tasks = closed ? "" : side;
}
let lastSig = "";
async function refreshTasks() {
  let r;
  try { r = await C.api("/api/tasks?limit=60"); } catch { return; }
  const running = r.jobs.filter((j) => j.status === "running").length, open = r.counts.open || 0;
  $("#tdOpen").textContent = open || ""; $("#tdBadge").textContent = (running + open) || "";
  const sig = JSON.stringify([tab, openTask, r.jobs.map((j) => [j.id, j.status, j.detail]), r.client_tasks.map((t) => [t.id, t.status, t.n_fragments])]);
  if (sig === lastSig) return;
  lastSig = sig;
  const b = $("#tdBody");
  if (tab === "jobs") {
    b.innerHTML = r.jobs.length ? r.jobs.map((j) => { const [cls, txt] = ST[j.status] || ST.running;
      return `<div class="td-item"><span class="st ${cls}" title="${txt}"></span><div class="td-main"><div class="td-title">${C.esc(j.title)}</div>
        <div class="td-meta">${C.esc(j.kind_title)} · ${txt} · ${ago(j.updated_at)}</div>${j.detail ? `<div class="td-detail">${C.esc(j.detail).slice(0, 220)}</div>` : ""}</div></div>`; }).join("")
      : '<div class="td-empty">还没有任务。对话监控归纳、整理星系、对话抽屉里的指令都会出现在这里。</div>';
  } else {
    b.innerHTML = r.client_tasks.length ? r.client_tasks.map((t) => { const [cls, txt] = ST[t.status] || ST.open;
      return `<div class="td-item click" data-id="${t.id}"><span class="st ${cls}" title="${txt}"></span><div class="td-main"><div class="td-title">${C.esc(t.title || t.task_key)}</div>
        <div class="td-meta">${C.esc(CLIENT[t.client] || t.client)} · ${C.esc(t.category || "未分类")} · ${txt} · ${t.n_fragments} 段 · ${ago(t.updated_at)}</div>
        ${openTask === t.id ? `<div class="td-detail" id="tdDet">读取中…</div>` : t.last_fragment ? `<div class="td-detail">${C.esc(t.last_fragment).slice(0, 120)}</div>` : ""}</div></div>`; }).join("")
      : '<div class="td-empty">还没有捕获到任务。打开「设置 → 对话监控」，在 Claude Code、Codex 等客户端里交代的多轮工作会记在这里。</div>';
    b.querySelectorAll(".td-item.click").forEach((el) => (el.onclick = (e) => { if (e.target.closest("button")) return; openTask = openTask === +el.dataset.id ? null : +el.dataset.id; lastSig = ""; refreshTasks(); }));
    if (openTask) showTask(openTask);
  }
}
async function showTask(id) {
  const t = await C.safe(() => C.api("/api/tasks/client/" + id));
  const el = $("#tdDet");
  if (!t || !el) return;
  el.innerHTML = t.fragments.map((f, i) => `<div class="frag">${i + 1}. ${C.esc(f.text)}</div>`).join("") +
    (t.result && t.status === "closed" ? `<div class="frag res">${C.esc(t.result).slice(0, 400)}</div>` : "") +
    (t.skill_name ? `<div class="frag">已存为技能：<b>${C.esc(t.skill_name)}</b></div>` : "") +
    `<div class="td-acts">${t.status !== "closed" ? '<button data-s="closed">标记完成</button>' : ""}${t.status !== "failed" ? '<button data-s="failed">标记失败</button>' : ""}${t.status !== "open" ? '<button data-s="open">重新打开</button>' : ""}</div>`;
  el.querySelectorAll("[data-s]").forEach((b) => (b.onclick = () => C.safe(async () => { b.disabled = true;
    await C.api("/api/tasks/client/" + id, { method: "POST", body: { status: b.dataset.s } }); lastSig = ""; refreshTasks(); C.refresh?.(); })));
}
