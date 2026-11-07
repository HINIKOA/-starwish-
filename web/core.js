// 星魂 SoulStar —— 核心记忆模块页面（查看 / 编辑三段 / 锁定 / 提案 / 版本历史与回滚 / 主人指令）
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—");

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}
let tt;
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = err ? "err" : "";
  clearTimeout(tt); tt = setTimeout(() => t.classList.add("hidden"), 3500);
}
async function safe(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

const HINTS = {
  identity: "AI 是谁、叫什么名字、性格与说话方式。",
  owner: "主人是谁、怎么称呼主人（建议写一行「称呼：…」）。",
  preferences: "主人长期稳定的喜好与禁忌，一行一条。",
};
const SRC = { panel: "面板", api: "API·主人", mcp: "MCP·主人", "proxy-command": "对话 /核心", "proxy-natural": "对话·主人原话",
  proposal: "批准提案", rollback: "回滚", undo: "撤销", v1: "旧版本", command: "指令" };
let C = null;

async function load() {
  C = await api("/api/core");
  renderTop(); renderEdit();
  const b = $("#pBadge"); b.textContent = C.pending; b.classList.toggle("hidden", !C.pending);
}

function renderTop() {
  $("#cmeta").innerHTML = `<span class="chip">版本 <b>r${C.rev}</b></span><span class="chip">每轮注入 <b>${C.tokens}</b> tok</span>
    <span class="chip">更新于 <b>${fmtDate(C.updated_at)}</b></span>`;
  const lb = $("#lockBtn");
  lb.textContent = C.locked ? "🔒 已上锁 · 点此解锁" : "🔓 未上锁 · 点此上锁";
  lb.classList.toggle("is-locked", !!C.locked);
  $("#intro").innerHTML = `核心记忆是星魂的<b>灵魂</b>：三段恒定内容，每轮对话都会注入。<b>后台 LLM 整理、导入、AI 自己的推测永远不能直接改它</b>，只能提「提案」。
    主人可以在这里编辑，也可以在任何 AI 客户端里直接下指令（<code>/核心 …</code> 或「记住，以后叫我X」），立即生效。
    每次修改都带 前/后/来源 记入永久日志，随时可以撤销或回到任意版本。
    ${C.locked ? '<br><span class="locked">🔒 当前已上锁（冻结）：所有直接修改都会被拒绝，远程主人指令会自动转为提案。</span>' : ""}`;
}

function renderEdit() {
  const L = C.locked;
  $("#tab-edit").innerHTML = `<div class="secgrid">${Object.entries(C.sections).map(([k, v], i) => `
    <div class="sec ${L ? "locked" : ""}">
      <h3><span class="num">${i + 1}</span>${esc(v.title)}</h3>
      <div class="hint">${HINTS[k]}</div>
      <textarea id="t_${k}" ${L ? "disabled" : ""} placeholder="（空）">${esc(v.content)}</textarea>
      <div class="foot"><input id="n_${k}" placeholder="修改说明（可选，记入日志）" ${L ? "disabled" : ""}>
        <button class="primary" data-save="${k}" ${L ? "disabled" : ""}>保存</button></div>
      <div class="dirty hidden" id="d_${k}">● 有未保存的修改</div>
    </div>`).join("")}</div>
    <div class="preview"><label>实际注入给模型的文本（约 ${C.tokens} tokens）</label><pre>${esc(C.text || "（三段都为空，暂不注入）")}</pre></div>`;
  for (const k of Object.keys(C.sections)) {
    $("#t_" + k).addEventListener("input", () => $("#d_" + k).classList.toggle("hidden", $("#t_" + k).value === C.sections[k].content));
  }
  $$("[data-save]").forEach((b) => (b.onclick = () => {
    const k = b.dataset.save;
    if (!confirm(`确认修改「${C.sections[k].title}」？改动会记入永久日志，可随时撤销。`)) return;
    safe(async () => {
      await api("/api/core", { method: "PUT", body: { section: k, content: $("#t_" + k).value, note: $("#n_" + k).value, confirm: true } });
      toast("已保存，版本 +1"); await load();
    });
  }));
}

$("#lockBtn").onclick = () => {
  if (!confirm(C.locked ? "解锁后，主人可以编辑，主人指令也会直接生效。确认解锁？" : "上锁后所有直接修改都会被拒绝（远程指令转为提案）。确认上锁？")) return;
  safe(async () => { await api("/api/core/lock", { method: "POST", body: { locked: !C.locked, confirm: true } }); toast(C.locked ? "已解锁" : "已上锁"); await load(); refreshTab(); });
};

// ---------------- 提案 ----------------
async function renderProposals() {
  const list = await safe(() => api("/api/proposals?status=pending"));
  if (!list) return;
  const el = $("#tab-proposals");
  el.innerHTML = `<div class="intro" style="margin-top:12px">提案来自 LLM 后台整理、导入、AI 的 <code>propose_core_change</code>，以及上锁 / 未授权时的主人指令。批准前可以先改内容。</div>
    ${list.length ? "" : '<div class="empty">暂无待批准的提案。</div>'}
    ${list.map((p) => `<div class="hcard">
      <div class="hhead"><span class="rev">#${p.id}</span><span class="src">${esc(p.section_title)}</span>
        <span class="src">${p.mode === "replace" ? "替换整段" : "追加一行"}</span><span class="t">${fmtDate(p.created_at)} · ${esc(p.source)}</span>
        <div class="acts"><button class="primary" data-ok="${p.id}" ${C.locked ? "disabled" : ""}>批准</button><button class="danger" data-no="${p.id}">拒绝</button></div></div>
      ${p.reason ? `<div class="note">理由：${esc(p.reason)}</div>` : ""}
      <textarea id="pc_${p.id}" rows="3" style="margin-top:8px">${esc(p.content)}</textarea></div>`).join("")}`;
  $$("[data-ok]").forEach((b) => (b.onclick = () => confirm("批准后写入核心记忆，确认？") && safe(async () => {
    await api(`/api/proposals/${b.dataset.ok}/approve`, { method: "POST", body: { confirm: true, content: $("#pc_" + b.dataset.ok).value } });
    toast("已批准"); await load(); renderProposals();
  })));
  $$("[data-no]").forEach((b) => (b.onclick = () => safe(async () => {
    await api(`/api/proposals/${b.dataset.no}/reject`, { method: "POST" }); toast("已拒绝"); await load(); renderProposals();
  })));
}

// ---------------- 历史 ----------------
function diffHtml(x) {
  return x.changed.map((s, i) => `<div class="diff"><div class="dt">${esc(x.changed_titles[i])}</div>${(x.diff[s] || []).map(([op, line]) =>
    `<div class="l ${op === "+" ? "add" : op === "-" ? "del" : "eq"}">${op === "=" ? "  " : op + " "}${esc(line) || "&nbsp;"}</div>`).join("")}</div>`).join("");
}
async function renderHistory() {
  const list = await safe(() => api("/api/core/history?limit=200"));
  if (!list) return;
  const el = $("#tab-history");
  el.innerHTML = `<div class="intro" style="margin-top:12px">全部来自只追加的事件日志 <code>events.jsonl</code>，永不删除。「回到此版本」= 恢复到那次修改之后的样子；「撤销这次」= 只把那次改动的分区恢复成修改前。回滚本身也会生成新版本，所以回滚也能被撤销。</div>
    ${list.length ? "" : '<div class="empty">还没有修改记录。</div>'}
    ${list.map((x) => x.kind === "lock" ? `<div class="hcard"><div class="hhead"><span class="rev">${x.locked ? "🔒" : "🔓"}</span>
        <span class="t">${esc((x.ts || "").replace("T", " ").slice(0, 19))} · ${x.locked ? "上锁" : "解锁"}（r${x.rev ?? "?"}）</span></div></div>` : `
      <div class="hcard">
        <div class="hhead"><span class="rev">${x.rev != null ? "r" + x.rev : "旧#" + x.legacy_no}</span>
          <span class="src s-${esc(x.source)}">${esc(SRC[x.source] || x.source)}</span>
          <span class="t">${esc((x.ts || "").replace("T", " ").slice(0, 19))}</span>
          <div class="acts">
            ${x.rev != null ? `<button data-to="${x.rev}" ${C.locked ? "disabled" : ""}>回到此版本</button>` : ""}
            <button class="danger" data-undo="${x.rev ?? ""}" data-legacy="${x.legacy_no ?? ""}" ${C.locked ? "disabled" : ""}>撤销这次</button>
          </div></div>
        ${x.note ? `<div class="note">说明：${esc(x.note)}</div>` : ""}
        ${x.instruction ? `<div class="note">主人原话：「${esc(x.instruction)}」</div>` : ""}
        ${diffHtml(x)}
      </div>`).join("")}`;
  const go = (body, msg) => confirm(msg) && safe(async () => {
    const r = await api("/api/core/rollback", { method: "POST", body: { ...body, confirm: true } });
    toast(r.applied ? `已生成新版本 r${r.rev}` : "内容没有变化"); await load(); renderHistory();
  });
  $$("[data-to]").forEach((b) => (b.onclick = () => go({ rev: +b.dataset.to, mode: "to" }, `回到 r${b.dataset.to}？（会生成一个新版本，可再撤销）`)));
  $$("[data-undo]").forEach((b) => (b.onclick = () => go(b.dataset.undo !== "" ? { rev: +b.dataset.undo, mode: "undo" } : { legacy_no: +b.dataset.legacy, mode: "undo" }, "撤销这次修改？")));
}

// ---------------- 主人指令 ----------------
function renderCommand() {
  const s = C.settings || {};
  $("#tab-command").innerHTML = `
    <div class="intro" style="margin-top:12px">在任何接入星魂代理的客户端（ChatGPT 类客户端 / DeepSeek / Hermes / 其他 OpenAI 兼容工具）里直接对 AI 说，星魂会在转发前就地执行；
      Claude 等 MCP 客户端用 <code>update_core</code> 工具（必须附上主人原话）。斜杠指令由星魂直接回复，不消耗模型 token。</div>
    <table class="cheat">
      <tr><td>/核心</td><td>查看当前核心记忆</td></tr>
      <tr><td>/核心 主人 九天，称呼天天</td><td>替换整段（分区：身份 / 主人 / 喜好）</td></tr>
      <tr><td>/核心 喜好 + 爱喝冰美式</td><td>追加一行</td></tr>
      <tr><td>/核心 称呼 = 天天</td><td>只改「称呼：」这一行（「名字 = 小星」改 AI 名字）</td></tr>
      <tr><td>/核心 历史 · /核心 撤销 · /核心 回到 r3</td><td>查看历史、撤销最近一次、回到某版本</td></tr>
      <tr><td>记住，以后叫我天天</td><td>自然语言：改称呼（并照常转给模型，让它自然确认）</td></tr>
      <tr><td>记住，你以后叫小星 · 记住，我不吃香菜</td><td>改 AI 名字 · 追加喜好</td></tr>
    </table>
    <label style="margin-top:16px">在这里试一条指令（以主人身份执行）</label>
    <div class="cmdbox"><input id="cmdIn" placeholder="例如：记住，以后叫我天天"><button class="primary" id="cmdGo">执行</button></div>
    <pre class="reply hidden" id="cmdOut"></pre>
    <h3 style="color:var(--accent2);font-size:14px;margin-top:22px">谁可以直接改</h3>
    <div class="opts">
      <label><input type="checkbox" id="o_chat" ${s.chat_commands !== false ? "checked" : ""}> 代理识别 /核心 指令</label>
      <label><input type="checkbox" id="o_nat" ${s.natural_commands !== false ? "checked" : ""}> 代理识别「记住，以后叫我X」</label>
      <label><input type="checkbox" id="o_mcp" ${s.mcp_owner_edit !== false ? "checked" : ""}> MCP update_core 带主人原话时直接生效</label>
      <div></div>
    </div>
    <label>主人令牌（远程 API 用 <code>Authorization: Bearer 令牌</code> 调 /api/core/update；留空 = 只信任本机）</label>
    <div class="cmdbox"><input id="o_tok" placeholder="${s.owner_token_set ? "已设置（" + esc(s.owner_token) + "），留空保持不变" : "未设置"}"><button id="o_save">保存设置</button></div>`;
  $("#cmdGo").onclick = () => safe(async () => {
    const r = await api("/api/core/command", { method: "POST", body: { text: $("#cmdIn").value } });
    const o = $("#cmdOut"); o.textContent = r.reply; o.classList.remove("hidden"); await load();
  });
  $("#cmdIn").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#cmdGo").click(); });
  $("#o_save").onclick = () => safe(async () => {
    const core = { chat_commands: $("#o_chat").checked, natural_commands: $("#o_nat").checked, mcp_owner_edit: $("#o_mcp").checked };
    if ($("#o_tok").value.trim()) core.owner_token = $("#o_tok").value.trim();
    await api("/api/settings", { method: "PUT", body: { core } }); toast("已保存"); await load(); renderCommand();
  });
}

// ---------------- 标签页 ----------------
let tab = "edit";
function refreshTab() { ({ edit: renderEdit, proposals: renderProposals, history: renderHistory, command: renderCommand })[tab](); }
$$(".ctabs button").forEach((b) => (b.onclick = () => {
  tab = b.dataset.tab;
  $$(".ctabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((x) => x.classList.toggle("hidden", x.id !== "tab-" + tab));
  history.replaceState(null, "", "#" + tab); refreshTab();
}));

(async () => {
  await safe(load);
  const h = location.hash.slice(1);
  if (["proposals", "history", "command"].includes(h)) $(`.ctabs [data-tab="${h}"]`).click();
})();
