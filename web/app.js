// 星愿 StarWish —— 记忆星图面板（无需构建，原生 ES Module + three.js）
// 设计灵感来自 dsh-xingxi（作者 Yuuyuko-uu）：越新的星越亮，老星只是变暗、不会消失。

import { initSettings, toggleSettings } from "./settings.js";
import { initDrawers } from "./drawers.js";

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

  // 一开始只有主核星辰：不再铺背景星尘，星空随记忆一颗颗长出来
  window.addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });
  bindControls(renderer.domElement);
}

// 核心记忆：一颗金色的主核星辰（外加一圈柔光），三段核心记忆都在它里面
function buildCore(THREE) {
  if (S.corePoints) { S.scene.remove(S.corePoints); S.corePoints.geometry.dispose(); }
  const secs = ["identity", "owner", "preferences"];
  const filled = S.core ? secs.filter((k) => (S.core.sections[k].content || "").trim()).length : 0;
  const lum = 0.55 + 0.15 * filled;                       // 核心记忆填得越满，主核越亮
  const pos = new Float32Array([0, 0, 0, 0, 0, 0]);
  const col = new Float32Array([1, 0.84, 0.42, 1, 0.72, 0.28]);
  const size = new Float32Array([16, 46]), br = new Float32Array([lum + 0.2, 0.22 + 0.06 * filled]);
  const hi = new Float32Array([0, 0]), ph = new Float32Array([0, 1.7]);
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
// 大小与亮度按时间先后：越新越大越亮，老星变小变暗但不会消失；融合出来的星按「代数」更大，颜色偏白紫。
function buildStars(THREE, list) {
  if (S.points) { S.scene.remove(S.points); S.points.geometry.dispose(); }
  const prevPos = S.prevPos || new Map(), first = !S.knownIds;
  S.stars = list; S.idToIdx = new Map();
  const n = list.length;
  const pos = new Float32Array(n * 3), col = new Float32Array(n * 3), size = new Float32Array(n),
    br = new Float32Array(n), hi = new Float32Array(n), ph = new Float32Array(n);
  const base = new Float32Array(n), final = new Float32Array(n * 3);
  const tagHue = new Map();
  const c = new THREE.Color();
  const now = Date.now() / 1000;
  S.anims = []; const ghosts = [];
  list.forEach((s, i) => {
    const [id, created, bright, imp, tag0, , gen = 0, fused = []] = s;
    const gcat = S.galaxy?.cat?.[id];
    const tag = gcat ? gcat[0] : tag0;          // v0.11.0：颜色按恒星系（大类）
    S.idToIdx.set(id, i);
    const f = n > 1 ? (n - 1 - i) / (n - 1) : 0; // 0 = 最新，1 = 最老
    const rr = rnd(hash(id) + 1);
    const spread = 22 + 73 * Math.min(1, n / 300);                       // 星少时聚在主核周围，星多了再铺开
    const R = 7 + spread * Math.sqrt(f) + rr() * 3;
    const arms = 5, arm = tag ? hash(tag) % arms : arms - 1;
    const ang = (arm / arms) * Math.PI * 2 + R * 0.075 + (rr() - 0.5) * 0.7;
    const gp = S.galaxy?.pos?.[id];              // v0.11.0：银河系布局（恒星系绕星系核、行星系聚在恒星系旁、星球绕行星系）
    const p = gp ? [gp[0], gp[1], gp[2]] : [Math.cos(ang) * R, (rr() - 0.5) * (1.5 + R * 0.12), Math.sin(ang) * R];
    final.set(p, i * 3); pos.set(p, i * 3);
    if (!tagHue.has(tag)) tagHue.set(tag, tag ? (hash(tag) % 360) / 360 : 0.62);
    c.setHSL(tagHue.get(tag), tag ? 0.55 : 0.35, 0.72);
    if (gen > 0) c.lerp(new THREE.Color(0.86, 0.8, 1.0), Math.min(0.7, 0.35 + gen * 0.15));
    col.set([c.r, c.g, c.b], i * 3);
    const ageDays = Math.max(0, (now - created) / 86400);
    const recency = Math.max(1 - f, Math.pow(0.5, ageDays / 7));          // 时间顺序 + 真实时间，取较新的那个
    size[i] = (1.8 + 5.2 * Math.pow(recency, 1.3)) * (0.85 + imp * 0.06) * (1 + 0.3 * gen);
    base[i] = Math.min(1, 0.14 + 0.66 * Math.pow(recency, 1.1) + 0.2 * Math.min(1, bright / 1.5));
    ph[i] = rr() * 6.28;
    // 诞生 / 融合动画（首次打开页面不播）
    if (!first && !S.knownIds.has(id)) {
      const from = fused.length ? fused.map((m) => prevPos.get(m)).filter(Boolean) : [];
      if (from.length) {
        from.forEach((fp) => ghosts.push({ from: fp, to: p, color: [c.r, c.g, c.b] }));
        S.anims.push({ i, from: from[0], to: p, t0: performance.now() + 900, dur: 600, flash: 1, kind: "fuse" });
        pos.set(from[0], i * 3);
      } else {
        S.anims.push({ i, from: [0, 0, 0], to: p, t0: performance.now(), dur: 1600, flash: 1, kind: "birth" });
        pos.set([0, 0, 0], i * 3);
      }
    }
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
  S.positions = final; S.baseBright = base;
  S.knownIds = new Set(list.map((x) => x[0]));
  S.prevPos = new Map(list.map((x, i) => [x[0], [final[i * 3], final[i * 3 + 1], final[i * 3 + 2]]]));
  // 被吸收的成员：旧位置也记着，后面再融合时还能从它们那里飞过来
  if (n) { S.minT = list[0][1]; S.maxT = list[n - 1][1]; }
  if (first && !S.userZoomed) S.cam.dist = 45 + 75 * Math.min(1, n / 300);  // 星少时镜头拉近
  const sysList = (S.galaxy && S.galaxy.systems) || [];
  if (first && !S.userZoomed && sysList.length) {   // 银河系布局：镜头退到能看见全部恒星系
    const ext = Math.max(...sysList.map((x) => Math.hypot(x.center[0], x.center[2]) + x.radius));
    S.cam.dist = Math.max(55, Math.min(240, ext * 1.15));
  }
  buildGhosts(THREE, ghosts);
  buildLabels(THREE, (S.galaxy && S.galaxy.systems) || []);
  $("#emptyHint").classList.toggle("hidden", n > 0);
  applyFilters();
}

// 融合动画：被吸收的星从原位置飞向新星，然后新星一闪
function buildGhosts(THREE, ghosts) {
  if (S.ghostPoints) { S.scene.remove(S.ghostPoints); S.ghostPoints.geometry.dispose(); S.ghostPoints = null; }
  if (!ghosts.length) return;
  const m = ghosts.length, pos = new Float32Array(m * 3), col = new Float32Array(m * 3), size = new Float32Array(m).fill(3.2),
    br = new Float32Array(m).fill(0.9), hi = new Float32Array(m), ph = new Float32Array(m);
  ghosts.forEach((gh, k) => { pos.set(gh.from, k * 3); col.set(gh.color, k * 3); });
  const g = new THREE.BufferGeometry();
  for (const [k, a, d] of [["position", pos, 3], ["aColor", col, 3], ["aSize", size, 1], ["aBright", br, 1], ["aHi", hi, 1], ["aPhase", ph, 1]]) g.setAttribute(k, new THREE.BufferAttribute(a, d));
  S.ghostPoints = new THREE.Points(g, makeMaterial(THREE));
  S.ghostPoints.userData = { ghosts, t0: performance.now(), dur: 1000 };
  S.scene.add(S.ghostPoints);
}

function stepAnims(now) {
  const ease = (x) => 1 - Math.pow(1 - x, 3);
  if (S.ghostPoints) {
    const { ghosts, t0, dur } = S.ghostPoints.userData; const k = Math.min(1, (now - t0) / dur);
    const pa = S.ghostPoints.geometry.getAttribute("position"), ba = S.ghostPoints.geometry.getAttribute("aBright");
    ghosts.forEach((gh, j) => { for (let d = 0; d < 3; d++) pa.array[j * 3 + d] = gh.from[d] + (gh.to[d] - gh.from[d]) * ease(k); ba.array[j] = 0.9 * (1 - k * 0.6); });
    pa.needsUpdate = true; ba.needsUpdate = true;
    if (k >= 1) { S.scene.remove(S.ghostPoints); S.ghostPoints.geometry.dispose(); S.ghostPoints = null; }
  }
  if (!S.anims || !S.anims.length || !S.points) return;
  const pa = S.points.geometry.getAttribute("position"), hi = S.points.geometry.getAttribute("aHi");
  S.anims = S.anims.filter((a) => {
    const k = Math.max(0, Math.min(1, (now - a.t0) / a.dur));
    for (let d = 0; d < 3; d++) pa.array[a.i * 3 + d] = a.from[d] + (a.to[d] - a.from[d]) * ease(k);
    hi.array[a.i] = k < 1 ? 0.6 + 0.4 * Math.sin(k * Math.PI) : 0;
    return k < 1;
  });
  pa.needsUpdate = true; hi.needsUpdate = true;
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
    if (!(S.anims && S.anims.some((a) => a.i === i))) hi.array[i] = i === S.hovered || i === S.selected ? 1 : 0;
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
    S.cam.dist = Math.min(600, Math.max(6, S.cam.dist * (e.deltaY > 0 ? 1.1 : 0.9))); S.userZoomed = true;
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
    const secs = ["identity", "owner", "preferences"];
    tt.innerHTML = `<b>✦ 主核星辰 · 核心记忆</b>` + secs.map((k, i) => { const txt = S.core ? S.core.sections[k].content : "";
      return `<div><span class="meta">${CORE_TITLES[i]}：</span>${esc((txt || "（空）").slice(0, 60))}</div>`; }).join("") + `<div class="meta">点击打开核心记忆模块</div>`;
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
  stepAnims(performance.now());
  for (const p of [S.points, S.corePoints, S.ghostPoints]) if (p) p.material.uniforms.uTime.value = t / 1000;
  S.renderer.render(S.scene, S.camera);
}

// ---------------- 数据加载 ----------------
// ---------------- 恒星系 / 行星系名字（v0.11.0） ----------------
function textSprite(THREE, text, big) {
  const cv = document.createElement("canvas"), ctx = cv.getContext("2d"), fs = big ? 44 : 30;
  ctx.font = `${big ? 600 : 400} ${fs}px "PingFang SC","Microsoft YaHei",sans-serif`;
  const w = Math.ceil(ctx.measureText(text).width) + 24;
  cv.width = w; cv.height = fs + 20;
  ctx.font = `${big ? 600 : 400} ${fs}px "PingFang SC","Microsoft YaHei",sans-serif`;
  ctx.fillStyle = big ? "rgba(255,226,160,0.95)" : "rgba(200,210,255,0.8)";
  ctx.shadowColor = "rgba(0,0,0,0.8)"; ctx.shadowBlur = 8;
  ctx.fillText(text, 12, fs + 4);
  const tex = new THREE.CanvasTexture(cv);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, depthTest: false }));
  const k = big ? 0.05 : 0.034;
  sp.scale.set(cv.width * k, cv.height * k, 1);
  return sp;
}
function buildLabels(THREE, systems) {
  if (S.labels) { S.scene.remove(S.labels); S.labels.traverse((o) => { o.material?.map?.dispose(); o.material?.dispose(); }); }
  S.labels = new THREE.Group();
  for (const sys of systems) {
    const a = textSprite(THREE, `${sys.name} · ${sys.count}`, true);
    a.position.set(sys.center[0], sys.center[1] + 4 + Math.min(10, sys.radius * 0.4), sys.center[2]);
    S.labels.add(a);
    for (const mn of sys.minors || []) {
      const b = textSprite(THREE, mn.name, false);
      b.position.set(mn.center[0], mn.center[1] + 2.4, mn.center[2]);
      S.labels.add(b);
    }
  }
  S.scene.add(S.labels);
}

async function loadMap() {
  const [m, core] = await Promise.all([api("/api/stars/map"), api("/api/core")]);
  S.core = core;
  S.galaxy = m.galaxy || null;
  const sig = m.stars.map((x) => x[0]).join(",") + "|" + (core.rev ?? "") + "|" + Math.floor(Date.now() / 3600000) + "|" +
    ((m.galaxy && m.galaxy.systems) || []).map((x) => x.name + x.count + (x.minors || []).map((y) => y.name + y.count).join("/")).join(",");
  if (S.three && sig === S.mapSig) { updateTimelineText(); return; }   // 没有变化就不重建
  S.mapSig = sig;
  if (S.three) { buildCore(S.three); buildStars(S.three, m.stars); }
  else { S.stars = m.stars; }
  updateTimelineText();
}
async function loadStats() {
  const s = await api("/api/stats");
  const chips = [
    ["星星", s.total], ["今日新增", "+" + s.today], ["核心", s.core_tokens + " tok"], ["技能", (s.skills ? s.skills.total : 0)],
    ["每轮注入约", s.est_tokens_per_turn + " tok"],
    ["检索", (s.jieba ? "jieba" : "二元组") + (s.fts ? "+FTS5" : "") + (s.semantic ? "+语义·" + ({ small: "bge-small", m3: "BGE-M3", api: "Qwen3" }[s.semantic_model] || "") : "")],
  ];
  if (s.fused) chips.push(["融合星", s.fused]);
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

function openPanel(name) { if (name === "skills") { location.href = "/skills"; return; } if (name === "eye") { location.href = "/eye"; return; } ({ core: panelCore, proposals: panelProposals, add: panelAdd, import: panelImport, sync: panelSync, settings: panelSettings, events: panelEvents })[name](); }

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
      ${s.generation ? `<span>进化</span><span>第 ${s.generation} 代融合星 · 由 ${s.fused_from.length} 颗相似记忆归纳而成</span>` : ""}
    </div>
    ${s.members && s.members.length ? `<h3>融合前的记忆</h3>${s.members.map((m) => `<div class="card"><div class="meta">#${m.id} · ${fmtDay(m.created_at)}${m.generation ? ` · 第 ${m.generation} 代` : ""}</div>${esc(m.summary)}</div>`).join("")}
      <div class="row"><button id="sUnfuse">拆开还原</button></div>` : ""}
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
  if ($("#sUnfuse")) $("#sUnfuse").onclick = () => confirm("把这颗融合星拆开，还原成原来的几颗？") && safe(async () => { await api(`/api/stars/${id}/unfuse`, { method: "POST" }); toast("已拆开还原"); closeDrawer(); await refresh(); });
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
    <h3>星辰融合</h3>
    <div class="sub">相似的记忆会融合、归纳成一颗新星（配好 LLM 时由它归纳，否则朴素拼接）。原来的星不会删除，可随时拆开还原。每新增 8 颗星自动检查一次。</div>
    <div class="row"><button id="fusePrev">看看哪些会融合</button><button class="primary" id="fuseGo">立即融合归纳</button></div>
    <div id="fuseBox"></div>
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
  $("#fusePrev").onclick = () => safe(async () => { const r = await api("/api/stars/fuse/preview");
    $("#fuseBox").innerHTML = r.groups.map((g) => `<div class="card"><div class="meta">${g.ids.length} 颗 · 相似度 ${g.score}</div>${g.summaries.map(esc).join("<br>")}</div>`).join("") || '<div class="sub">现在没有足够相似的星。</div>'; });
  $("#fuseGo").onclick = () => safe(async () => { $("#fuseGo").disabled = true; $("#fuseGo").textContent = "融合中…";
    const r = await api("/api/stars/fuse", { method: "POST", body: {} }); toast(r.fused.length ? `融合了 ${r.fused.length} 组` : "现在没有足够相似的星"); closeDrawer(); await refresh(); });
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
function panelSettings() { toggleSettings(); }   // v0.8.0：设置改为独立窗口（web/settings.js），点「设置」开 / 关

// —— 向量模型状态卡 ——
const EMB_NAME = { small: "内置 bge-small-zh", m3: "BGE-M3", api: "Qwen3-Embedding 接口" };
async function embedPanel() {
  const box = $("#embBox"); if (!box) return;
  const st = await safe(() => api("/api/embed/status")); if (!st || !$("#embBox")) return;
  const mb = (b) => (b / 1e6).toFixed(1) + "MB";
  const job = (j) => !j ? "" : j.state === "running" ? ` · 下载中 ${Math.round((100 * j.done) / (j.total || 1))}%` : j.state === "error" ? ` · <span class="warn">下载失败：${esc(j.error)}</span>` : "";
  const L = st.local, cov = st.coverage, busy = Object.values(st.jobs || {}).some((j) => j.state === "running") || st.backfill.state === "running";
  box.innerHTML = `
    <div class="card"><div>正在使用：<b>${st.active ? EMB_NAME[st.active] : "无（只用字面检索）"}</b>${st.note ? ` · <span class="warn">${esc(st.note)}</span>` : ""}</div>
      <div class="meta">已有向量 ${cov.done}/${cov.total} 颗星${st.backfill.state === "running" ? " · 正在后台补齐…" : ""}${st.deps_ok ? "" : ` · <span class="warn">缺少运行库（onnxruntime / tokenizers / numpy，约 ${st.deps_mb}MB）</span>`}</div></div>
    <div class="card"><b>① 内置 bge-small-zh int8</b> · 512 维 · ${L.small.installed ? `<span class="okc">已就绪 ${mb(L.small.bytes)}</span>` : `<span class="warn">未找到，需下载 ${L.small.need_mb}MB</span>`}${job(L.small.job)}
      <div class="meta">常驻默认，完整包自带；中文为主，CPU 上每条约 10 毫秒。</div></div>
    <div class="card"><b>② BGE-M3 int8</b> · 1024 维 · ${L.m3.installed ? `<span class="okc">已部署 ${mb(L.m3.bytes)}</span>` : `未安装（约 ${L.m3.need_mb}MB）`}${job(L.m3.job)}
      <div class="meta">中英等 100+ 语言、长文本更强；占内存约 1GB，放在程序目录 models/embed/m3，不进灵魂包。</div></div>
    <div class="card"><b>③ Qwen3-Embedding 接口</b> · ${st.api.configured ? `<span class="okc">已填写（${esc(st.api.model)}${st.api.dimensions ? " · " + st.api.dimensions + " 维" : ""}）</span>` : "未选择接口模型（在「默认模型」里选）"}${st.api.error ? ` · <span class="warn">${esc(st.api.error)}</span>` : ""}
      <div class="meta">在「模型服务」添加服务商（如阿里云百炼、硅基流动）并同步模型后，到「默认模型」选向量模型。按量计费，记忆内容会发给接口方。</div></div>
    <div class="row">
      ${!st.deps_ok ? '<button id="embDeps">一键安装运行库</button>' : ""}
      ${!L.small.installed ? '<button id="embSmall">下载内置模型</button>' : ""}
      ${L.m3.installed ? '<button class="danger" id="embM3Del">删除 BGE-M3</button>' : `<button class="primary" id="embM3">一键部署 BGE-M3（约 ${L.m3.need_mb}MB）</button>`}
      <button id="embTest">测试向量接口</button><button id="embRe">重新补齐向量</button>
    </div>`;
  const inst = (kind) => safe(async () => { await api("/api/embed/install", { method: "POST", body: { kind } }); toast("已在后台开始"); embedPanel(); });
  if ($("#embDeps")) $("#embDeps").onclick = () => inst("small");
  if ($("#embSmall")) $("#embSmall").onclick = () => inst("small");
  if ($("#embM3")) $("#embM3").onclick = () => confirm(`下载 BGE-M3（约 ${L.m3.need_mb}MB）到程序目录？下载完在上面选「②」并保存即可切换。`) && inst("m3");
  if ($("#embM3Del")) $("#embM3Del").onclick = () => confirm("删除 BGE-M3 模型文件？（向量还留在灵魂包里，重新部署后可直接用）") && safe(async () => { await api("/api/embed/models/m3", { method: "DELETE" }); toast("已删除"); embedPanel(); });
  $("#embTest").onclick = () => safe(async () => { const r = await api("/api/embed/test", { method: "POST" }); toast(`接口可用：${r.dim} 维 · ${r.ms} 毫秒 · 「羽绒服」与「保暖外套」相似度 ${r.sim}`); embedPanel(); });
  $("#embRe").onclick = () => safe(async () => { await api("/api/embed/backfill", { method: "POST" }); toast("已在后台补齐"); embedPanel(); });
  if (busy) setTimeout(embedPanel, 2000);
}

// —— 事件日志 ——
const EV = { star_add: "新星", star_edit: "编辑星", embed_switch: "换向量模型", embed_model_download: "下载向量模型", embed_install: "部署向量模型", star_fuse: "融合", star_unfuse: "拆开", star_merge: "合并", star_soft_delete: "隐藏", star_restore: "恢复", star_brighten: "变亮", star_dim: "变暗",
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
  initSettings({ api, toast, esc, safe, refresh, embedPanel });
  initDrawers({ api, toast, esc, safe, refresh });
  if (location.hash.startsWith("#settings")) toggleSettings(true, location.hash.slice(10));   // 例如 #settings/data/webdav
  const THREE = await loadThree();
  if (THREE) { S.three = THREE; initScene(THREE); requestAnimationFrame(animate); }
  else $("#noThree").classList.remove("hidden");
  await refresh();
  setInterval(() => safe(loadStats), 30000);
  setInterval(() => { if (document.visibilityState === "visible" && $("#drawer").classList.contains("hidden")) safe(loadMap); }, 20000);  // 新记忆在使用中一颗颗长出来
  window.__starwish = S; // 调试用
})();

// 桌面版心跳：页面全部关闭后，后台服务才会自动退出（仅浏览器兜底模式用到）
setInterval(() => fetch("/api/ping", { method: "POST" }).catch(() => {}), 15000); fetch("/api/ping", { method: "POST" }).catch(() => {});
