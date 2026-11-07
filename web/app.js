// 星魂 SoulStar —— 记忆星图面板（无需构建，原生 ES Module + three.js）
// 设计灵感来自 dsh-xingxi（作者 Yuuyuko-uu）：越新的星越亮，老星只是变暗、不会消失。

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—");
const fmtDay = (t) => (t ? new Date(t * 1000).toLocaleDateString("zh-CN") : "—");

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.body && !(o.body instanceof FormData) && typeof o.body !== "string") {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) {
    const msg = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}
let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = err ? "err" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}
async function safe(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

// ---------------- three.js 加载（本地优先，CDN 兜底） ----------------
async function loadThree() {
  const urls = [
    "/static/vendor/three.module.min.js",
    "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js",
    "https://unpkg.com/three@0.160.0/build/three.module.min.js",
  ];
  for (const u of urls) { try { return await import(u); } catch (e) { /* 下一个 */ } }
  return null;
}

// ---------------- 状态 ----------------
const S = {
  three: null, renderer: null, scene: null, camera: null, points: null, corePoints: null,
  stars: [], idToIdx: new Map(), positions: null, baseBright: null,
  searchSet: null, cutoff: Infinity, hovered: -1, selected: -1,
  cam: { theta: 0.6, phi: 1.05, dist: 120, target: [0, 0, 0] },
  targetGoal: null, autoRotate: true, lastInteract: 0, minT: 0, maxT: 0, core: null,
};

const hash = (s) => { let h = 2166136261; s = String(s); for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; };
const rnd = (seed) => { let x = seed || 1; return () => { x ^= x << 13; x ^= x >>> 17; x ^= x << 5; return ((x >>> 0) % 100000) / 100000; }; };

// ---------------- 着色器 ----------------
const VERT = `
attribute float aSize; attribute float aBright; attribute float aHi; attribute float aPhase; attribute vec3 aColor;
uniform float uPR; uniform float uTime;
varying vec3 vColor; varying float vAlpha; varying float vHi;
void main(){
  vec4 mv = modelViewMatrix * vec4(position,1.0);
  float tw = 0.88 + 0.12*sin(uTime*1.3 + aPhase);
  vAlpha = aBright * tw; vColor = aColor; vHi = aHi;
  float s = aSize * (1.0 + aHi*1.4) * (0.55 + aBright*0.9);
  gl_PointSize = s * uPR * (420.0 / max(1.0, -mv.z));
  gl_Position = projectionMatrix * mv;
}`;
const FRAG = `
varying vec3 vColor; varying float vAlpha; varying float vHi;
void main(){
  vec2 c = gl_PointCoord - 0.5; float d = length(c);
  if (d > 0.5) discard;
  float glow = smoothstep(0.5, 0.0, d);
  float core = smoothstep(0.14, 0.0, d);
  vec3 col = mix(vColor, vec3(1.0), core*0.8 + vHi*0.3);
  gl_FragColor = vec4(col*(glow*0.9+core), (glow*0.75+core) * vAlpha);
}`;

function makeMaterial(THREE) {
  return new THREE.ShaderMaterial({
    vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: { uPR: { value: Math.min(2, window.devicePixelRatio || 1) }, uTime: { value: 0 } },
  });
}

function initScene(THREE) {
  const el = $("#stage");
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setClearColor(0x03040a, 1);
  el.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x03040a, 0.0035);
  const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 3000);
  Object.assign(S, { renderer, scene, camera });

  // 背景星尘
  const N = 2500, pos = new Float32Array(N * 3), r = rnd(7);
  for (let i = 0; i < N; i++) {
    const R = 300 + r() * 700, th = r() * Math.PI * 2, ph = Math.acos(2 * r() - 1);
    pos[i * 3] = R * Math.sin(ph) * Math.cos(th); pos[i * 3 + 1] = R * Math.cos(ph); pos[i * 3 + 2] = R * Math.sin(ph) * Math.sin(th);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  scene.add(new THREE.Points(g, new THREE.PointsMaterial({ color: 0x5a6390, size: 1.2, sizeAttenuation: false, transparent: true, opacity: 0.55 })));

  window.addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });
  bindControls(renderer.domElement);
}

// 核心记忆：中央明亮星团（三颗主星 + 金色星尘）
function buildCore(THREE) {
  if (S.corePoints) { S.scene.remove(S.corePoints); S.corePoints.geometry.dispose(); }
  const secs = ["identity", "owner", "preferences"];
  const N = 3 + 160, pos = new Float32Array(N * 3), col = new Float32Array(N * 3), size = new Float32Array(N),
    br = new Float32Array(N), hi = new Float32Array(N), ph = new Float32Array(N);
  const r = rnd(42);
  for (let i = 0; i < N; i++) {
    if (i < 3) {
      const a = (i / 3) * Math.PI * 2;
      pos.set([Math.cos(a) * 2.6, (i - 1) * 0.6, Math.sin(a) * 2.6], i * 3);
      const filled = S.core && S.core.sections[secs[i]].content.trim();
      col.set([1, 0.86, 0.55], i * 3); size[i] = 12; br[i] = filled ? 1 : 0.35;
    } else {
      const R = Math.abs((r() + r() + r()) / 3 - 0.5) * 9, th = r() * Math.PI * 2, y = (r() - 0.5) * 2.2;
      pos.set([Math.cos(th) * R, y, Math.sin(th) * R], i * 3);
      col.set([1, 0.8 + r() * 0.15, 0.5 + r() * 0.3], i * 3); size[i] = 1.2 + r() * 1.6; br[i] = 0.35 + r() * 0.5;
    }
    ph[i] = r() * 6.28;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setAttribute("aColor", new THREE.BufferAttribute(col, 3));
  g.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
  g.setAttribute("aBright", new THREE.BufferAttribute(br, 1));
  g.setAttribute("aHi", new THREE.BufferAttribute(hi, 1));
  g.setAttribute("aPhase", new THREE.BufferAttribute(ph, 1));
  S.corePoints = new THREE.Points(g, makeMaterial(THREE));
  S.corePoints.userData.isCore = true;
  S.scene.add(S.corePoints);
}

// 记忆星：一个 Points 对象承载全部星星（1 万+ 依然流畅）
function buildStars(THREE, list) {
  if (S.points) { S.scene.remove(S.points); S.points.geometry.dispose(); }
  S.stars = list; S.idToIdx = new Map();
  const n = list.length;
  const pos = new Float32Array(n * 3), col = new Float32Array(n * 3), size = new Float32Array(n),
    br = new Float32Array(n), hi = new Float32Array(n), ph = new Float32Array(n);
  const base = new Float32Array(n);
  const tagHue = new Map();
  const c = new THREE.Color();
  list.forEach((s, i) => {
    const [id, created, bright, imp, tag] = s;
    S.idToIdx.set(id, i);
    const f = n > 1 ? (n - 1 - i) / (n - 1) : 0; // 0 = 最新，1 = 最老
    const rr = rnd(hash(id) + 1);
    const R = 8 + 85 * Math.sqrt(f) + rr() * 4;
    const arms = 5, arm = tag ? hash(tag) % arms : arms - 1;
    const ang = (arm / arms) * Math.PI * 2 + R * 0.075 + (rr() - 0.5) * 0.7;
    pos[i * 3] = Math.cos(ang) * R;
    pos[i * 3 + 1] = (rr() - 0.5) * (1.5 + R * 0.12);
    pos[i * 3 + 2] = Math.sin(ang) * R;
    if (!tagHue.has(tag)) tagHue.set(tag, tag ? (hash(tag) % 360) / 360 : 0.62);
    c.setHSL(tagHue.get(tag), tag ? 0.55 : 0.35, 0.72);
    col.set([c.r, c.g, c.b], i * 3);
    size[i] = 1.6 + imp * 0.55;
    base[i] = 0.16 + 0.84 * Math.min(1, bright / 1.5);
    ph[i] = rr() * 6.28;
  });
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setAttribute("aColor", new THREE.BufferAttribute(col, 3));
  g.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
  g.setAttribute("aBright", new THREE.BufferAttribute(br, 1));
  g.setAttribute("aHi", new THREE.BufferAttribute(hi, 1));
  g.setAttribute("aPhase", new THREE.BufferAttribute(ph, 1));
  g.computeBoundingSphere();
  S.points = new THREE.Points(g, makeMaterial(THREE));
  S.scene.add(S.points);
  S.positions = pos; S.baseBright = base;
  if (n) { S.minT = list[0][1]; S.maxT = list[n - 1][1]; }
  applyFilters();
}

function visible(i) { return S.stars[i][1] <= S.cutoff; }

function applyFilters() {
  if (!S.points) return;
  const br = S.points.geometry.getAttribute("aBright"), hi = S.points.geometry.getAttribute("aHi");
  for (let i = 0; i < S.stars.length; i++) {
    let b = S.baseBright[i];
    if (!visible(i)) b = 0;
    else if (S.searchSet && !S.searchSet.has(S.stars[i][0])) b *= 0.07;
    if ((i === S.selected || i === S.hovered) && visible(i)) b = Math.max(b, 0.95);
    br.array[i] = b;
    hi.array[i] = i === S.hovered || i === S.selected ? 1 : 0;
  }
  br.needsUpdate = true; hi.needsUpdate = true;
}

// ---------------- 相机控制（拖拽旋转 / 滚轮缩放） ----------------
function bindControls(dom) {
  let drag = null;
  dom.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, moved: 0 }; S.lastInteract = performance.now(); });
  window.addEventListener("pointerup", (e) => {
    if (drag && drag.moved < 5 && e.target === dom) onClick(e);
    drag = null;
  });
  dom.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.moved += Math.abs(dx) + Math.abs(dy);
      S.cam.theta -= dx * 0.005; S.cam.phi = Math.min(3.0, Math.max(0.15, S.cam.phi - dy * 0.005));
      drag.x = e.clientX; drag.y = e.clientY; S.lastInteract = performance.now();
      $("#tooltip").classList.add("hidden");
    } else onHover(e);
  });
  dom.addEventListener("wheel", (e) => {
    e.preventDefault();
    S.cam.dist = Math.min(600, Math.max(6, S.cam.dist * (e.deltaY > 0 ? 1.1 : 0.9)));
    S.lastInteract = performance.now();
  }, { passive: false });
}

function pick(e) {
  const THREE = S.three;
  const mouse = new THREE.Vector2((e.clientX / window.innerWidth) * 2 - 1, -(e.clientY / window.innerHeight) * 2 + 1);
  const rc = new THREE.Raycaster();
  rc.setFromCamera(mouse, S.camera);
  rc.params.Points.threshold = Math.max(0.6, S.cam.dist * 0.012);
  const objs = [S.points, S.corePoints].filter(Boolean);
  let best = null;
  for (const hit of rc.intersectObjects(objs, false)) {
    const isCore = hit.object.userData.isCore;
    if (isCore && hit.index >= 3) continue;
    if (!isCore && (!visible(hit.index) || (S.searchSet && !S.searchSet.has(S.stars[hit.index][0])))) continue;
    const score = hit.distanceToRay / Math.max(1, hit.distance) - (isCore ? 0.01 : 0);
    if (!best || score < best.score) best = { ...hit, isCore, score };
  }
  return best;
}

const CORE_TITLES = ["AI身份与名字", "主人是谁及称呼", "主人的喜好"];
function onHover(e) {
  if (!S.points && !S.corePoints) return;
  const h = pick(e);
  const tt = $("#tooltip");
  const prev = S.hovered;
  if (!h) { S.hovered = -1; tt.classList.add("hidden"); $("#stage").style.cursor = "default"; }
  else if (h.isCore) {
    S.hovered = -1;
    const key = ["identity", "owner", "preferences"][h.index];
    const txt = S.core ? S.core.sections[key].content : "";
    tt.innerHTML = `<b>核心 · ${CORE_TITLES[h.index]}</b><div>${esc((txt || "（空，点击填写）").slice(0, 120))}</div>`;
    showTip(e); $("#stage").style.cursor = "pointer";
  } else {
    S.hovered = h.index;
    const s = S.stars[h.index];
    tt.innerHTML = `<div>${esc(s[5])}</div><div class="meta">#${s[0]} · ${fmtDay(s[1])}${s[4] ? " · " + esc(s[4]) : ""} · 亮度 ${s[2].toFixed(2)}</div>`;
    showTip(e); $("#stage").style.cursor = "pointer";
  }
  if (prev !== S.hovered) applyFilters();
}
function showTip(e) {
  const tt = $("#tooltip");
  tt.classList.remove("hidden");
  tt.style.left = Math.min(window.innerWidth - 340, e.clientX + 14) + "px";
  tt.style.top = e.clientY + 14 + "px";
}
function onClick(e) {
  const h = pick(e);
  if (!h) return;
  if (h.isCore) openPanel("core");
  else openStar(S.stars[h.index][0]);
}

function flyTo(id) {
  const i = S.idToIdx.get(id);
  if (i === undefined) return;
  S.targetGoal = [S.positions[i * 3], S.positions[i * 3 + 1], S.positions[i * 3 + 2]];
  S.cam.dist = Math.min(S.cam.dist, 40);
  S.selected = i; applyFilters();
}

function animate(t) {
  requestAnimationFrame(animate);
  if (!S.renderer) return;
  const idle = performance.now() - S.lastInteract > 2500;
  if (S.autoRotate && idle && $("#drawer").classList.contains("hidden")) S.cam.theta += 0.0007;
  if (S.targetGoal) {
    const tg = S.cam.target;
    for (let k = 0; k < 3; k++) tg[k] += (S.targetGoal[k] - tg[k]) * 0.08;
    if (Math.hypot(...tg.map((v, k) => v - S.targetGoal[k])) < 0.05) S.targetGoal = null;
  }
  const { theta, phi, dist, target } = S.cam;
  S.camera.position.set(target[0] + dist * Math.sin(phi) * Math.cos(theta), target[1] + dist * Math.cos(phi), target[2] + dist * Math.sin(phi) * Math.sin(theta));
  S.camera.lookAt(target[0], target[1], target[2]);
  for (const p of [S.points, S.corePoints]) if (p) p.material.uniforms.uTime.value = t / 1000;
  S.renderer.render(S.scene, S.camera);
}

// ---------------- 数据加载 ----------------
async function loadMap() {
  const [m, core] = await Promise.all([api("/api/stars/map"), api("/api/core")]);
  S.core = core;
  if (S.three) { buildCore(S.three); buildStars(S.three, m.stars); }
  else { S.stars = m.stars; }
  updateTimelineText();
}
async function loadStats() {
  const s = await api("/api/stats");
  const chips = [
    ["星星", s.total], ["今日新增", "+" + s.today], ["核心", s.core_tokens + " tok"], ["技能", (s.skills ? s.skills.total : 0)],
    ["每轮注入约", s.est_tokens_per_turn + " tok"],
    ["检索", (s.jieba ? "jieba" : "二元组") + (s.fts ? "+FTS5" : "") + (s.semantic ? "+语义" : "")],
  ];
  if (s.deleted) chips.push(["已隐藏", s.deleted]);
  $("#stats").innerHTML = chips.map(([k, v]) => `<span class="chip">${k} <b>${esc(v)}</b></span>`).join("");
  const b = $("#propBadge");
  b.textContent = s.pending_proposals; b.classList.toggle("hidden", !s.pending_proposals);
}
async function refresh() { await safe(loadMap); await safe(loadStats); }

// ---------------- 搜索 / 时间线 ----------------
let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  searchTimer = setTimeout(async () => {
    const dd = $("#searchResults");
    if (!q) { S.searchSet = null; dd.classList.add("hidden"); applyFilters(); return; }
    const res = await safe(() => api("/api/search?q=" + encodeURIComponent(q)));
    if (!res) return;
    S.searchSet = new Set(res.map((r) => r.id));
    applyFilters();
    dd.innerHTML = res.length
      ? `<div class="item meta">命中 ${res.length} 颗</div>` + res.slice(0, 12).map((r) => `<div class="item" data-id="${r.id}">${esc(r.summary)}<div class="meta">#${r.id} · ${fmtDay(r.created_at)} · ${esc(r.tags.join(" "))}</div></div>`).join("")
      : `<div class="item meta">没有找到</div>`;
    dd.classList.remove("hidden");
  }, 280);
});
$("#searchResults").addEventListener("click", (e) => {
  const it = e.target.closest(".item[data-id]");
  if (!it) return;
  const id = +it.dataset.id; flyTo(id); openStar(id); $("#searchResults").classList.add("hidden");
});
document.addEventListener("click", (e) => { if (!e.target.closest(".searchbox")) $("#searchResults").classList.add("hidden"); });

function updateTimelineText() {
  const v = +$("#tlSlider").value;
  if (v >= 1000 || !S.stars.length) { S.cutoff = Infinity; $("#tlText").textContent = "全部"; }
  else { S.cutoff = S.minT + (S.maxT - S.minT) * (v / 1000); $("#tlText").textContent = "截至 " + fmtDay(S.cutoff); }
  applyFilters();
}
$("#tlSlider").addEventListener("input", updateTimelineText);
$("#resetView").addEventListener("click", () => { S.cam = { theta: 0.6, phi: 1.05, dist: 120, target: [0, 0, 0] }; S.targetGoal = null; S.selected = -1; applyFilters(); });
$("#autoRotate").addEventListener("change", (e) => (S.autoRotate = e.target.checked));

// ---------------- 抽屉面板 ----------------
function openDrawer(html) { $("#drawerBody").innerHTML = html; $("#drawer").classList.remove("hidden"); }
function closeDrawer() { $("#drawer").classList.add("hidden"); S.selected = -1; applyFilters(); }
$("#drawerClose").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => openPanel(b.dataset.panel)));

function openPanel(name) { if (name === "skills") { location.href = "/skills"; return; } ({ core: panelCore, proposals: panelProposals, add: panelAdd, import: panelImport, sync: panelSync, settings: panelSettings, events: panelEvents })[name](); }

// —— 星星详情 ——
async function openStar(id) {
  const s = await safe(() => api("/api/stars/" + id));
  if (!s) return;
  const i = S.idToIdx.get(id); if (i !== undefined) { S.selected = i; applyFilters(); }
  const del = !!s.deleted_at;
  openDrawer(`
    <h2>星 #${s.id} ${del ? '<span class="warn">（已隐藏）</span>' : ""}</h2>
    <div class="bright-bar"><i style="width:${Math.min(100, (s.brightness / 1.5) * 100)}%"></i></div>
    <div class="kv">
      <span>亮度</span><span>${s.brightness.toFixed(3)}（手动增益 ×${s.boost.toFixed(2)}）</span>
      <span>创建</span><span>${fmtDate(s.created_at)}</span>
      <span>最近召回</span><span>${fmtDate(s.last_recalled_at)}</span>
      <span>召回次数</span><span>${s.recall_count}</span>
      <span>来源</span><span>${esc(s.source || "—")}</span>
    </div>
    <label>摘要（召回时只发送摘要，省 token）</label><input id="sSummary" value="${esc(s.summary)}">
    <label>全文</label><textarea id="sContent" rows="7">${esc(s.content)}</textarea>
    <div class="grid2">
      <div><label>标签（逗号分隔）</label><input id="sTags" value="${esc(s.tags.join(", "))}"></div>
      <div><label>重要度</label><select id="sImp">${[1, 2, 3, 4, 5].map((v) => `<option ${v === s.importance ? "selected" : ""}>${v}</option>`).join("")}</select></div>
    </div>
    <div class="row">
      <button class="primary" id="sSave">保存</button>
      <button id="sBright">☀ 变亮</button><button id="sDim">☾ 变暗</button>
      ${del ? '<button id="sRestore">恢复</button>' : '<button class="danger" id="sDel">隐藏（软删除）</button>'}
    </div>
    <div class="sub" style="margin-top:8px">星星永远不会被真正删除：隐藏只是打标记，所有改动都记在事件日志里。</div>
    ${s.raw ? `<h3>原始对话</h3><pre>${esc(s.raw)}</pre>` : ""}
  `);
  const act = (p, msg) => safe(async () => { await api(`/api/stars/${id}/${p}`, { method: "POST" }); toast(msg); await refresh(); openStar(id); });
  $("#sSave").onclick = () => safe(async () => {
    await api("/api/stars/" + id, { method: "PATCH", body: { summary: $("#sSummary").value, content: $("#sContent").value, tags: $("#sTags").value, importance: +$("#sImp").value } });
    toast("已保存"); await refresh(); openStar(id);
  });
  $("#sBright").onclick = () => act("brighten", "变亮了");
  $("#sDim").onclick = () => act("dim", "变暗了");
  if ($("#sDel")) $("#sDel").onclick = () => confirm("隐藏这颗星？（可随时恢复）") && act("delete", "已隐藏");
  if ($("#sRestore")) $("#sRestore").onclick = () => act("restore", "已恢复");
}

// —— 核心记忆：独立模块页面 /core ——
function panelCore() { location.href = "/core"; }

// —— 提案 ——
async function panelProposals(showAll = false) {
  const list = await safe(() => api("/api/proposals?status=" + (showAll ? "all" : "pending")));
  if (!list) return;
  openDrawer(`
    <h2>核心记忆提案</h2>
    <div class="sub">来自 LLM 整理、MCP 工具或导入。批准前可以先改内容。批准需要核心记忆处于未上锁状态。</div>
    <div class="row"><button id="pAll">${showAll ? "只看待处理" : "查看全部历史"}</button></div>
    ${list.length ? "" : '<div class="sub" style="margin-top:14px">暂无提案。</div>'}
    ${list.map((p) => `
      <div class="card">
        <div class="meta">#${p.id} · ${esc(p.section_title)} · ${p.mode === "replace" ? "替换整段" : "追加"} · ${fmtDate(p.created_at)} · ${esc(p.source)} ${p.status !== "pending" ? "· <b>" + (p.status === "approved" ? "已批准" : "已拒绝") + "</b>" : ""}</div>
        ${p.reason ? `<div class="sub">理由：${esc(p.reason)}</div>` : ""}
        <textarea id="pc_${p.id}" rows="4" ${p.status !== "pending" ? "disabled" : ""}>${esc(p.content)}</textarea>
        ${p.status === "pending" ? `<div class="row"><button class="primary" data-ok="${p.id}">批准</button><button class="danger" data-no="${p.id}">拒绝</button></div>` : ""}
      </div>`).join("")}
  `);
  $("#pAll").onclick = () => panelProposals(!showAll);
  document.querySelectorAll("[data-ok]").forEach((b) => (b.onclick = () => {
    const id = b.dataset.ok;
    if (!confirm("批准后会修改核心记忆，确认？")) return;
    safe(async () => { await api(`/api/proposals/${id}/approve`, { method: "POST", body: { confirm: true, content: $("#pc_" + id).value } }); toast("已批准并写入核心记忆"); await refresh(); panelProposals(showAll); });
  }));
  document.querySelectorAll("[data-no]").forEach((b) => (b.onclick = () => safe(async () => { await api(`/api/proposals/${b.dataset.no}/reject`, { method: "POST" }); await refresh(); panelProposals(showAll); })));
}

// —— 新增星 ——
function panelAdd() {
  openDrawer(`
    <h2>点亮一颗新星</h2>
    <label>内容</label><textarea id="aContent" rows="6" placeholder="想让 AI 长期记住的事"></textarea>
    <label>摘要（可留空，自动生成）</label><input id="aSummary">
    <div class="grid2">
      <div><label>标签</label><input id="aTags" placeholder="例如：工作, 家人"></div>
      <div><label>重要度</label><select id="aImp"><option>1</option><option>2</option><option selected>3</option><option>4</option><option>5</option></select></div>
    </div>
    <label class="tl-check" style="margin-top:12px"><input type="checkbox" id="aMerge" checked> 与近似的旧星合并</label>
    <div class="row"><button class="primary" id="aGo">保存</button></div>
  `);
  $("#aGo").onclick = () => safe(async () => {
    const r = await api("/api/stars", { method: "POST", body: { content: $("#aContent").value, summary: $("#aSummary").value, tags: $("#aTags").value, importance: +$("#aImp").value, source: "panel", merge_near: $("#aMerge").checked } });
    toast(r.merged ? `已合并到 #${r.id}` : r.duplicate ? `已存在 #${r.id}` : `新星 #${r.id} 已点亮`);
    await refresh(); flyTo(r.id); openStar(r.id);
  });
}

// —— 导入 ——
function panelImport() {
  openDrawer(`
    <h2>导入</h2>
    <div class="sub">支持：星汐(dsh-xingxi) 档案文件夹（记忆卡.md / 日记 / 原始对话）、普通 markdown/txt、ChatGPT 导出的 conversations.json。
    记忆卡第一章「我是谁」与「对方是谁」会变成<b>核心记忆提案</b>，其余条目变成星星；重复内容自动跳过。</div>
    <h3>按本机路径导入</h3>
    <label>文件或文件夹路径（例如 D:\\档案\\某某）</label><input id="iPath">
    <label>类型</label><select id="iKind"><option value="auto">自动识别</option><option value="xingxi">星汐档案</option><option value="text">markdown / txt</option><option value="chatgpt">ChatGPT conversations.json</option></select>
    <div class="row"><button class="primary" id="iGo">导入</button></div>
    <h3>上传文件</h3>
    <div class="sub">星汐档案可先打包成 .zip 上传。</div>
    <input type="file" id="iFile" multiple>
    <label>类型</label><select id="iKind2"><option value="auto">自动识别</option><option value="xingxi">星汐档案 zip</option><option value="text">markdown / txt</option><option value="chatgpt">ChatGPT conversations.json</option></select>
    <div class="row"><button class="primary" id="iUp">上传并导入</button></div>
    <pre id="iRes" class="hidden"></pre>
  `);
  const show = (r) => { const p = $("#iRes"); p.textContent = JSON.stringify(r, null, 2); p.classList.remove("hidden"); };
  $("#iGo").onclick = () => safe(async () => { const r = await api("/api/import/path", { method: "POST", body: { path: $("#iPath").value, kind: $("#iKind").value } }); show(r); toast("导入完成"); refresh(); });
  $("#iUp").onclick = () => safe(async () => {
    const files = $("#iFile").files; const out = [];
    for (const f of files) { const fd = new FormData(); fd.append("file", f); fd.append("kind", $("#iKind2").value); out.push({ file: f.name, ...(await api("/api/import/upload", { method: "POST", body: fd })) }); }
    show(out); toast("导入完成"); refresh();
  });
}

// —— 备份 / 整理 ——
async function panelSync() {
  const [local, st] = await Promise.all([safe(() => api("/api/sync/snapshots")), safe(() => api("/api/maintenance/status"))]);
  const pending = st ? Object.values(st.pending).reduce((a, b) => a + b, 0) : 0;
  openDrawer(`
    <h2>备份与整理</h2>
    <h3>LLM 整理（可选）</h3>
    <div class="sub">${st && st.llm_ready ? "已配置 API Key。" : '<span class="warn">未配置 API Key：整理功能不可用，其余功能不受影响。</span>'}
      待整理的归档对话：<b>${pending}</b> 条。上次整理：${st && st.last ? fmtDate(st.last.t) + `（新增 ${st.last.added}，合并 ${st.last.merged}，提案 ${st.last.proposals}）` : "—"}</div>
    <div class="row"><button class="primary" id="mGo" ${st && st.llm_ready ? "" : "disabled"}>立即整理</button></div>
    <h3>本地快照</h3>
    <div class="sub">保存在 soul/snapshots/（不含 config.json）。恢复前会自动再做一次快照，可回退。</div>
    <div class="row"><button id="snapGo">立即快照</button></div>
    ${(local || []).map((s) => `<div class="card"><div class="meta">${esc(s.name)} · ${(s.size / 1024).toFixed(1)} KB</div><button data-rl="${esc(s.name)}">从此快照恢复</button></div>`).join("") || '<div class="sub">暂无快照</div>'}
    <h3>WebDAV 云备份</h3>
    <div class="sub">在「设置」里填写地址、账号和备份口令（口令用于加密，未填则明文上传）。上次备份：${st && st.last_backup ? fmtDate(st.last_backup) : "—"}</div>
    <div class="row"><button class="primary" id="davGo">立即备份</button><button id="davList">查看云端版本</button></div>
    <div id="davBox"></div>
  `);
  if ($("#mGo")) $("#mGo").onclick = () => safe(async () => { await api("/api/maintenance/consolidate", { method: "POST" }); toast("整理已在后台开始"); });
  $("#snapGo").onclick = () => safe(async () => { const r = await api("/api/sync/snapshot", { method: "POST" }); toast("快照：" + r.name); panelSync(); });
  document.querySelectorAll("[data-rl]").forEach((b) => (b.onclick = () => {
    if (!confirm("用这个快照覆盖当前灵魂包？（会先自动备份当前状态）")) return;
    const pw = b.dataset.rl.endsWith(".enc") ? prompt("备份口令") : "";
    safe(async () => { await api("/api/sync/restore", { method: "POST", body: { name: b.dataset.rl, source: "local", passphrase: pw, confirm: true } }); toast("已恢复"); refresh(); });
  }));
  $("#davGo").onclick = () => safe(async () => { const r = await api("/api/sync/backup", { method: "POST" }); toast("已上传 " + r.name + (r.warning ? " · " + r.warning : ""), !!r.warning); });
  $("#davList").onclick = () => safe(async () => {
    const list = await api("/api/sync/remote");
    $("#davBox").innerHTML = list.map((s) => `<div class="card"><div class="meta">${esc(s.name)} · ${(s.size / 1024).toFixed(1)} KB</div><button data-rr="${esc(s.name)}">从云端恢复</button></div>`).join("") || '<div class="sub">云端暂无备份</div>';
    document.querySelectorAll("[data-rr]").forEach((b) => (b.onclick = () => {
      if (!confirm("用云端版本覆盖当前灵魂包？（会先自动做本地快照）")) return;
      safe(async () => { await api("/api/sync/restore", { method: "POST", body: { name: b.dataset.rr, source: "remote", confirm: true } }); toast("已从云端恢复"); refresh(); });
    }));
  });
}

// —— 设置 ——
async function panelSettings() {
  const c = await safe(() => api("/api/settings"));
  if (!c) return;
  const inp = (path, label, type = "text", ph = "") => {
    const [a, b] = path.split(".");
    const v = c[a][b];
    if (type === "checkbox") return `<label class="tl-check" style="margin-top:10px"><input type="checkbox" data-k="${path}" ${v ? "checked" : ""}> ${label}</label>`;
    return `<label>${label}</label><input data-k="${path}" type="${type}" value="${type === "password" ? "" : esc(v)}" placeholder="${esc(ph)}">`;
  };
  const sec = (path, label) => { const [a, b] = path.split("."); return inp(path, label, "password", c[a][b + "_set"] ? "已设置（留空则不修改）" : "未设置"); };
  const origin = location.origin;
  openDrawer(`
    <h2>设置</h2>
    <div class="sub">配置保存在 <code>soul/config.json</code>。${c.secrets_encrypted ? (c.secrets_unlocked ? '<span class="okc">机密已加密，当前已解锁。</span>' : '<span class="warn">机密已加密，尚未解锁（API Key 等暂不可用）。</span>') : '<span class="warn">API Key 等目前以明文保存，可在下方开启主密码加密。</span>'}</div>
    <h3>接入地址</h3>
    <div class="kv"><span>OpenAI 兼容</span><span>${origin}/v1</span><span>REST API</span><span>${origin}/api</span><span>MCP</span><span>python mcp_server.py</span></div>
    <h3>上游大模型（代理转发 + 整理用）</h3>
    ${inp("llm.base_url", "Base URL", "text", "https://api.deepseek.com/v1")}
    ${sec("llm.api_key", "API Key")}
    <div class="grid2"><div>${inp("llm.model", "默认模型")}</div><div>${inp("llm.maintenance_model", "整理用模型（可空）")}</div></div>
    <h3>代理</h3>
    ${inp("proxy.inject", "自动注入核心记忆 + 相关星星", "checkbox")}
    ${inp("proxy.log_to_archive", "把对话写入归档，供日后整理", "checkbox")}
    <div class="grid2"><div>${inp("proxy.budget_tokens", "每轮召回预算（tokens）", "number")}</div><div>${inp("proxy.model_override", "强制模型（可空）")}</div></div>
    ${sec("proxy.proxy_key", "代理密钥（可空；填了则客户端需用它作为 API Key）")}
    <h3>记忆亮度</h3>
    <div class="grid2">
      <div>${inp("memory.half_life_days", "半衰期（天）", "number")}</div><div>${inp("memory.brightness_floor", "最低亮度", "number")}</div>
      <div>${inp("memory.brightness_weight", "亮度在排序中的权重", "number")}</div><div>${inp("memory.recall_budget_default", "默认召回预算", "number")}</div>
    </div>
    ${inp("memory.semantic", "启用语义检索（需另装 sentence-transformers）", "checkbox")}
    <h3>WebDAV</h3>
    ${inp("webdav.url", "地址", "text", "https://dav.jianguoyun.com/dav/")}
    <div class="grid2"><div>${inp("webdav.user", "账号")}</div><div>${sec("webdav.password", "密码 / 应用密码")}</div>
    <div>${inp("webdav.remote_dir", "远端目录")}</div><div>${inp("webdav.keep_versions", "保留版本数", "number")}</div></div>
    ${sec("webdav.passphrase", "备份口令（用于加密上传的快照，请牢记）")}
    <h3>定时任务（小时，0 = 关闭）</h3>
    <div class="grid2"><div>${inp("schedules.consolidate_hours", "LLM 整理", "number")}</div><div>${inp("schedules.backup_hours", "WebDAV 备份", "number")}</div>
    <div>${inp("schedules.snapshot_hours", "本地快照", "number")}</div><div>${inp("schedules.keep_local_snapshots", "本地快照保留数", "number")}</div></div>
    <div class="row"><button class="primary" id="setSave">保存设置</button></div>
    <h3>机密加密（可选）</h3>
    <div class="sub">用主密码加密 config.json 里的 API Key / WebDAV 密码 / 备份口令。启用后每次启动需在此解锁（或设置环境变量 SOULSTAR_MASTER）。</div>
    <input id="master" type="password" placeholder="主密码">
    <div class="row">
      ${c.secrets_encrypted ? '<button id="mUnlock">解锁</button><button class="danger" id="mOff">关闭加密</button>' : '<button id="mOn">启用加密</button>'}
    </div>
  `);
  $("#setSave").onclick = () => safe(async () => {
    const patch = {};
    document.querySelectorAll("[data-k]").forEach((el) => {
      const [a, b] = el.dataset.k.split(".");
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "number") v = v === "" ? 0 : Number(v);
      if (el.type === "password" && v === "") return;
      (patch[a] = patch[a] || {})[b] = v;
    });
    await api("/api/settings", { method: "PUT", body: patch }); toast("设置已保存"); refresh(); panelSettings();
  });
  if ($("#mOn")) $("#mOn").onclick = () => safe(async () => { await api("/api/secrets/encrypt", { method: "POST", body: { passphrase: $("#master").value } }); toast("已启用加密"); panelSettings(); });
  if ($("#mUnlock")) $("#mUnlock").onclick = () => safe(async () => { await api("/api/secrets/unlock", { method: "POST", body: { passphrase: $("#master").value } }); toast("已解锁"); panelSettings(); });
  if ($("#mOff")) $("#mOff").onclick = () => confirm("关闭加密后机密将以明文保存，确认？") && safe(async () => { await api("/api/secrets/decrypt", { method: "POST" }); panelSettings(); });
}

// —— 事件日志 ——
const EV = { star_add: "新星", star_edit: "编辑星", star_merge: "合并", star_soft_delete: "隐藏", star_restore: "恢复", star_brighten: "变亮", star_dim: "变暗",
  core_edit: "改核心(旧)", core_change: "改核心", skill_change: "技能修改", skill_staged: "技能待审", evolve_review: "技能巡检", skill_feedback: "技能反馈", core_lock: "锁定/解锁", proposal_create: "新提案", proposal_approve: "批准提案", proposal_reject: "拒绝提案", recall: "召回",
  recall_detail: "查看全文", archive_append: "归档", consolidate: "整理", snapshot_local: "快照", backup_webdav: "云备份", restore: "恢复快照", server_start: "启动" };
async function panelEvents() {
  const list = await safe(() => api("/api/events?limit=200"));
  if (!list) return;
  openDrawer(`<h2>事件日志</h2><div class="sub">soul/events.jsonl 只追加不修改，记录每一次变化，永久可追溯。</div>
    ${list.map((e) => `<div class="card"><div class="meta">${esc(e.ts)} · <b>${esc(EV[e.type] || e.type)}</b>${e.id ? " · #" + e.id : ""}</div>${esc(JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !["ts", "t", "type", "id"].includes(k))))).slice(0, 300)}</div>`).join("")}`);
}

// ---------------- 启动 ----------------
(async function main() {
  const THREE = await loadThree();
  if (THREE) { S.three = THREE; initScene(THREE); requestAnimationFrame(animate); }
  else $("#noThree").classList.remove("hidden");
  await refresh();
  setInterval(() => safe(loadStats), 30000);
  window.__soulstar = S; // 调试用
})();
