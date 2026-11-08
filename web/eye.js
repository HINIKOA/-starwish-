// 星愿 StarWish —— 宇宙之眼：在浏览器里采集屏幕 / 摄像头，算画面变化，挑关键帧，开口即问。
// 变化检测与选帧思路参考 dsh-see-now（Apache-2.0），框选区域 / 能力预检 / 预算参考 mcai（CC0）；本文件为独立实现。
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—");
async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.body && typeof o.body !== "string" && !(o.body instanceof FormData)) { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  let d = null; try { d = await r.json(); } catch { d = null; }
  if (!r.ok) throw new Error((d && (d.detail || d.message)) || `HTTP ${r.status}`);
  return d;
}
let tt;
function toast(m, err = false) { const t = $("#toast"); t.textContent = m; t.className = err ? "err" : ""; clearTimeout(tt); tt = setTimeout(() => t.classList.add("hidden"), 4200); }
async function safe(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

// ---------------- 状态 ----------------
let ST = null;               // /api/eye/status
let S = null;                // 当前观看会话
let media = null;            // MediaStream
let sampleTimer = null, liveTimer = null;
const video = $("#video");
const small = document.createElement("canvas"); small.width = 64; small.height = 36;
const sctx = small.getContext("2d", { willReadFrequently: true });
const big = document.createElement("canvas"); const bctx = big.getContext("2d");
let prev = null, integ = 0, lastKeep = 0, lastCut = 0;
let keys = [];               // 关键帧环形缓冲 [{t, url, cut}]
let crop = null;             // 归一化框选 {x,y,w,h}
let busy = false, ctrl = null;
const CFG = () => (ST && ST.settings) || {};

// ---------------- 变化检测（64×36 网格；亮度 + 色度差） ----------------
const SAMPLE_FPS = 6, CUT_SCORE = 0.38, INTEG_KEEP = 2.2, MIN_KEEP_GAP = 600, KEEP_SECONDS = 120, MAX_KEYS = 30;
function drawSrc(ctx, w, h) {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return false;
  const c = crop || { x: 0, y: 0, w: 1, h: 1 };
  ctx.drawImage(video, c.x * vw, c.y * vh, c.w * vw, c.h * vh, 0, 0, w, h);
  return true;
}
function sampleOnce() {
  if (!drawSrc(sctx, 64, 36)) return;
  const d = sctx.getImageData(0, 0, 64, 36).data;
  const n = 64 * 36, cur = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const r = d[i * 4], g = d[i * 4 + 1], b = d[i * 4 + 2];
    cur[i * 3] = 0.299 * r + 0.587 * g + 0.114 * b;      // 亮度
    cur[i * 3 + 1] = b - cur[i * 3];                      // 色度
    cur[i * 3 + 2] = r - cur[i * 3];
  }
  let score = 0;
  if (prev) {
    let changed = 0, mag = 0;
    for (let i = 0; i < n; i++) {
      const dy = Math.abs(cur[i * 3] - prev[i * 3]) / 255, dc = (Math.abs(cur[i * 3 + 1] - prev[i * 3 + 1]) + Math.abs(cur[i * 3 + 2] - prev[i * 3 + 2])) / 510;
      const v = dy * 0.7 + dc * 0.3; mag += v; if (v > 0.08) changed++;
    }
    score = 0.6 * (changed / n) + 0.4 * Math.min(1, (mag / n) * 4);
  }
  prev = cur;
  $("#meterBar").style.width = Math.min(100, score * 160) + "%";
  const now = performance.now();
  const isCut = score >= CUT_SCORE;
  integ += score;
  if ((isCut || integ >= INTEG_KEEP) && now - lastKeep > MIN_KEEP_GAP) {
    keep(isCut); integ = 0; lastKeep = now;
    if (isCut && now - lastCut > 3000) { lastCut = now; onSceneCut(); }
  }
}
function grab() {
  const w = Math.max(160, Math.min(1920, +CFG().frame_width || 768));
  const vw = video.videoWidth || 16, vh = video.videoHeight || 9;
  const c = crop || { w: 1, h: 1 };
  big.width = w; big.height = Math.max(2, Math.round(w * (c.h * vh) / (c.w * vw)));
  drawSrc(bctx, big.width, big.height);
  return big.toDataURL("image/jpeg", 0.72);
}
function keep(cut) {
  const t = Date.now();
  keys.push({ t, url: grab(), cut });
  keys = keys.filter((k) => t - k.t < KEEP_SECONDS * 1000).slice(-MAX_KEYS);
  renderStrip();
}
function renderStrip() {
  $("#strip").innerHTML = keys.slice(-10).map((k) => `<figure class="${k.cut ? "cut" : ""}"><img src="${k.url}"><figcaption>${Math.round((Date.now() - k.t) / 1000)}s</figcaption></figure>`).join("");
}

// ---------------- 选帧 + 拼分镜图 ----------------
function pickFrames() {
  const max = Math.max(1, +CFG().max_frames || 6);
  const now = { t: Date.now(), url: grab(), now: true };
  const pool = keys.filter((k) => now.t - k.t > 400);
  // 优先保留切场景帧，再按时间均匀补齐
  let chosen = pool.filter((k) => k.cut).slice(-(max - 1));
  const rest = pool.filter((k) => !chosen.includes(k));
  while (chosen.length < max - 1 && rest.length) chosen.push(rest.splice(Math.floor(rest.length / 2), 1)[0] || rest.pop());
  chosen = chosen.filter(Boolean).sort((a, b) => a.t - b.t).slice(-(max - 1));
  return [...chosen, now];
}
const loadImg = (u) => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = no; i.src = u; });
async function composeGrid(frames) {
  if (frames.length === 1) return frames[0].url;
  const n = frames.length, cols = n <= 2 ? n : n <= 4 ? 2 : 3, rows = Math.ceil(n / cols);
  const imgs = await Promise.all(frames.map((f) => loadImg(f.url)));
  const W = Math.max(28, Math.floor((+CFG().frame_width || 768) / 28) * 28);   // 宽高对齐 28 像素切块
  const tw = Math.floor(W / cols / 28) * 28 || 28, th = Math.max(28, Math.round(tw * imgs[0].height / imgs[0].width / 28) * 28);
  const cv = document.createElement("canvas"); cv.width = tw * cols; cv.height = th * rows;
  const cx = cv.getContext("2d"); cx.fillStyle = "#000"; cx.fillRect(0, 0, cv.width, cv.height);
  cx.font = `bold ${Math.max(12, Math.round(th / 9))}px sans-serif`;
  frames.forEach((f, i) => {
    const x = (i % cols) * tw, y = Math.floor(i / cols) * th;
    cx.drawImage(imgs[i], x, y, tw, th);
    const label = f.now ? "现在" : `${Math.round((Date.now() - f.t) / 1000)}秒前`;
    cx.fillStyle = "rgba(0,0,0,.6)"; cx.fillRect(x, y, cx.measureText(label).width + 12, Math.max(16, th / 7));
    cx.fillStyle = f.now ? "#ffd27a" : "#fff"; cx.fillText(label, x + 6, y + Math.max(13, th / 8.5));
  });
  return cv.toDataURL("image/jpeg", 0.72);
}
async function buildFrames() {
  const fr = pickFrames();
  const eng = ST.engine;
  if (eng === "frame" && (CFG().frame_compose || "grid") === "grid") return [await composeGrid(fr)];
  return fr.map((f) => f.url);
}

// ---------------- 采集 ----------------
async function startCapture(src) {
  if (media) await stopCapture(false);
  try {
    if (src === "camera") media = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 }, audio: false });
    else media = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 12 }, audio: true, preferCurrentTab: false, selfBrowserSurface: "exclude", surfaceSwitching: "include" });
  } catch (e) { toast("没有拿到画面：" + (e.message || e.name), true); media = null; return; }
  video.srcObject = media; await video.play().catch(() => {});
  media.getVideoTracks()[0].addEventListener("ended", () => stopCapture(true));
  S = await safe(() => api("/api/eye/session/start", { method: "POST", body: { title: $("#title").value, source: src, engine: ST.engine } }));
  if (!S) { await stopCapture(false); return; }
  keys = []; prev = null; integ = 0;
  $("#placeholder").classList.add("hidden"); $("#stopBtn").classList.remove("hidden"); $("#liveBadge").classList.remove("hidden");
  sampleTimer = setInterval(sampleOnce, 1000 / SAMPLE_FPS);
  const t0 = Date.now();
  liveTimer = setInterval(() => { const s = Math.floor((Date.now() - t0) / 1000); $("#liveTime").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; renderStrip(); }, 1000);
  sys(`开始一起看「${S.title}」。画面只在本机处理，${CFG().save_frames ? "已按设置保存画面" : "不保存画面"}，只记文字日记。`);
  if (voiceOn()) startVoice(src); else startSubtitles();
  if ($("#micChk").checked) startMic();
}
async function stopCapture(makeStar = true) {
  clearInterval(sampleTimer); clearInterval(liveTimer); sampleTimer = liveTimer = null;
  stopSubtitles(); stopVoice();
  if (ctrl) ctrl.abort();
  speechSynthesis.cancel();
  if (media) media.getTracks().forEach((t) => t.stop());
  media = null; video.srcObject = null;
  $("#placeholder").classList.remove("hidden"); $("#stopBtn").classList.add("hidden"); $("#liveBadge").classList.add("hidden");
  if (S) {
    const sid = S.id; S = null;
    const r = await safe(() => api(`/api/eye/session/${sid}/end`, { method: "POST", body: makeStar ? {} : { make_star: false } }));
    if (r) sys(r.star ? `已停止。这次观看沉淀成了一颗星 #${r.star.id}：${r.star.summary}` : "已停止，画面已全部丢弃。");
    if (r && r.voice && r.voice.cast) sys(`🌌 这次认识了：${r.voice.cast}${r.voice.saved ? `（已存进声纹库，下次看这部或同系列会直接认出）` : ""}`);
    loadStatus();
  }
}

// ---------------- 问答（开口即问，流式） ----------------
function addMsg(cls, text, meta = "") {
  const log = $("#log"); log.querySelector(".empty")?.remove();
  const el = document.createElement("div"); el.className = "msg " + cls;
  el.innerHTML = `<div class="b">${esc(text)}</div>${meta ? `<div class="m">${esc(meta)}</div>` : ""}`;
  log.appendChild(el); log.scrollTop = log.scrollHeight; return el;
}
const sys = (t) => addMsg("sys", t);
async function ask(text, mode = "user") {
  if (!S) { toast("先开始观看", true); return; }
  if (mode === "auto" && (busy || speaking())) return;
  if (ctrl) ctrl.abort();                 // 主人插话：打断上一轮
  stopSpeaking();
  busy = true; ctrl = new AbortController();
  if (mode === "user") addMsg("user", text);
  const t0 = performance.now();
  let el = null, out = "", audio = [];
  try {
    const frames = await buildFrames();
    const r = await fetch("/api/eye/ask", { method: "POST", signal: ctrl.signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: S.id, text, frames, mode }) });
    if ((r.headers.get("content-type") || "").includes("application/json")) {
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      return;  // 主动吐槽被限速跳过
    }
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await rd.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
        if (!line.startsWith("data:")) continue;
        const p = JSON.parse(line.slice(5));
        if (p.type === "delta") { out += p.text; if (!el && out.replace(/[….\s]/g, "")) el = addMsg("ai" + (mode === "auto" ? " auto" : ""), ""); if (el) el.querySelector(".b").textContent = out; }
        else if (p.type === "audio") audio.push(p.data);
        else if (p.type === "error") addMsg("sys", "⚠ " + p.message);
        else if (p.type === "info") $("#stat").textContent = p.message;
        else if (p.type === "names") onNames(p.changes);
        else if (p.type === "done") {
          const fm = p.first_ms ?? Math.round(performance.now() - t0);
          if (el) el.insertAdjacentHTML("beforeend", `<div class="m">${mode === "auto" ? "主动 · " : ""}首字 ${fm} ms · ${p.tokens} tok</div>`);
          $("#stat").textContent = `今天已用 ${p.today.tokens} tok / ${p.today.requests} 次 · 上一轮首字 ${fm} ms，全程 ${p.total_ms ?? "—"} ms`;
          if (!p.silent) { if (audio.length) playPcm(audio); else speak(p.text); }
          ST.usage = p.today; const chips = $("#emeta").querySelectorAll(".chip"); if (chips[2]) chips[2].innerHTML = `今天 <b>${p.today.tokens}</b> tok · <b>${p.today.requests}</b> 次`;
        }
      }
      $("#log").scrollTop = $("#log").scrollHeight;
    }
  } catch (e) { if (e.name !== "AbortError") addMsg("sys", "⚠ " + e.message); }
  finally { busy = false; }
}
function onSceneCut() { if (S && (CFG().pace || "normal") !== "off" && $("#paceSel").value !== "off") ask("", "auto"); }

// ---------------- 朗读（浏览器 TTS）与回声守卫 ----------------
let spokenText = "", pcmCtx = null, pcmEnd = 0;
const speaking = () => speechSynthesis.speaking || (pcmCtx && pcmCtx.currentTime < pcmEnd);
function stopSpeaking() { speechSynthesis.cancel(); if (pcmCtx) { pcmCtx.close(); pcmCtx = null; pcmEnd = 0; } }
function speak(text) {
  if (!$("#ttsChk").checked || !text || !("speechSynthesis" in window)) return;
  spokenText = text;
  const u = new SpeechSynthesisUtterance(text); u.lang = "zh-CN"; u.rate = 1.1;
  speechSynthesis.speak(u);
}
function playPcm(chunks) {  // 千问直接出语音（预留，未实测）：按 24kHz 16bit 单声道拼接播放
  if (!$("#ttsChk").checked) return;
  pcmCtx = new AudioContext({ sampleRate: 24000 });
  let bytes = chunks.map((c) => Uint8Array.from(atob(c), (ch) => ch.charCodeAt(0)));
  let all = new Uint8Array(bytes.reduce((a, b) => a + b.length, 0)); let o = 0; bytes.forEach((b) => { all.set(b, o); o += b.length; });
  if (all[0] === 0x52 && all[1] === 0x49) all = all.slice(44);   // 去掉 WAV 头
  const pcm = new Int16Array(all.buffer, 0, Math.floor(all.length / 2));
  const buf = pcmCtx.createBuffer(1, pcm.length, 24000); const ch = buf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const s = pcmCtx.createBufferSource(); s.buffer = buf; s.connect(pcmCtx.destination); s.start(); pcmEnd = pcmCtx.currentTime + buf.duration;
}

// ---------------- 主人语音（浏览器识别；AI 说话时说话即打断） ----------------
let rec = null;
function startMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { toast("这个浏览器不支持语音识别，可以用 Chrome / Edge，或者直接打字", true); $("#micChk").checked = false; return; }
  rec = new SR(); rec.lang = "zh-CN"; rec.continuous = true; rec.interimResults = true;
  rec.onresult = (ev) => {
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const t = ev.results[i][0].transcript.trim(); if (!t) continue;
      const echo = speaking() && spokenText && (spokenText.includes(t) || t.length < 2);
      if (echo) continue;                                   // 回声守卫：听到的是自己刚说的话
      if (speaking() && t.length >= 2) stopSpeaking();      // 主人插话 → 立刻闭嘴
      if (ev.results[i].isFinal) ask(t, "user");
      else $("#stat").textContent = "🎙 " + t;
    }
  };
  rec.onend = () => { if (rec && $("#micChk").checked) try { rec.start(); } catch {} };
  try { rec.start(); } catch {}
}
function stopMic() { if (rec) { const r = rec; rec = null; r.onend = null; r.stop(); } }

// ---------------- 电影声音转写（可选：OpenAI 兼容转写服务） ----------------
let subRec = null, subTimer = null;
function startSubtitles() {
  if (!media || CFG().stt_mode !== "api") return;
  const at = media.getAudioTracks(); if (!at.length) { sys("这次共享没有带声音，只能看画面。想带声音请选「标签页」并勾选「共享音频」。"); return; }
  const aud = new MediaStream(at);
  const cycle = () => {
    if (!media) return;
    const chunks = []; subRec = new MediaRecorder(aud, { mimeType: "audio/webm" });
    subRec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    subRec.onstop = async () => {
      if (!S || !chunks.length) return;
      const fd = new FormData(); fd.append("file", new Blob(chunks, { type: "audio/webm" }), "clip.webm"); fd.append("session", S.id); fd.append("kind", "subtitle");
      try { const r = await api("/api/eye/transcribe", { method: "POST", body: fd }); if (r.text) addMsg("sub", "♪ " + r.text); } catch (e) { $("#stat").textContent = "转写失败：" + e.message; }
    };
    subRec.start(); setTimeout(() => subRec && subRec.state === "recording" && subRec.stop(), 6000);
  };
  cycle(); subTimer = setInterval(cycle, 6200);
}
function stopSubtitles() { clearInterval(subTimer); subTimer = null; if (subRec && subRec.state === "recording") subRec.stop(); subRec = null; }

// ---------------- 框选区域 ----------------
function setupRegion() {
  const stage = $("#eyeStage"), box = $("#crop"); let sel = false, p0 = null;
  const rel = (e) => { const r = video.getBoundingClientRect(); const vw = video.videoWidth || 16, vh = video.videoHeight || 9;
    const s = Math.min(r.width / vw, r.height / vh), dw = vw * s, dh = vh * s, ox = r.left + (r.width - dw) / 2, oy = r.top + (r.height - dh) / 2;
    return { x: Math.min(1, Math.max(0, (e.clientX - ox) / dw)), y: Math.min(1, Math.max(0, (e.clientY - oy) / dh)), ox: ox - r.left, oy: oy - r.top, dw, dh }; };
  const show = (c, g) => { box.classList.remove("hidden"); Object.assign(box.style, { left: g.ox + c.x * g.dw + "px", top: g.oy + c.y * g.dh + "px", width: c.w * g.dw + "px", height: c.h * g.dh + "px" }); };
  $("#regionBtn").onclick = () => { if (!media) return toast("先开始观看", true); sel = true; stage.classList.add("selecting"); toast("在画面上拖动，框出想让它看的区域"); };
  $("#fullBtn").onclick = () => { crop = null; box.classList.add("hidden"); $("#fullBtn").classList.add("hidden"); prev = null; };
  stage.onpointerdown = (e) => { if (!sel) return; p0 = rel(e); stage.setPointerCapture(e.pointerId); };
  stage.onpointermove = (e) => { if (!sel || !p0) return; const p = rel(e); show({ x: Math.min(p0.x, p.x), y: Math.min(p0.y, p.y), w: Math.abs(p.x - p0.x), h: Math.abs(p.y - p0.y) }, p); };
  stage.onpointerup = (e) => { if (!sel || !p0) return; const p = rel(e); const c = { x: Math.min(p0.x, p.x), y: Math.min(p0.y, p.y), w: Math.abs(p.x - p0.x), h: Math.abs(p.y - p0.y) };
    sel = false; p0 = null; stage.classList.remove("selecting");
    if (c.w < 0.03 || c.h < 0.03) { box.classList.add("hidden"); return; }
    crop = c; prev = null; show(c, p); $("#fullBtn").classList.remove("hidden"); toast(`只看框内区域（约 ${Math.round(c.w * c.h * 100)}% 画面）`); };
}

// ---------------- 状态、日记、设置 ----------------
async function loadStatus() {
  ST = await api("/api/eye/status");
  await loadVoice();
  const e = ST.engines[ST.engine]; const c = CFG();
  $("#emeta").innerHTML = `<span class="chip">引擎 <b>${esc(e.title)}</b></span><span class="chip">模型 <b>${esc(e.model || "未设置")}</b></span>
    <span class="chip">今天 <b>${ST.usage.tokens}</b> tok · <b>${ST.usage.requests}</b> 次</span>${e.ready ? "" : '<span class="chip warn">未配置</span>'}`;
  $("#intro").innerHTML = `宇宙之眼让 AI <b>和你一起看</b>：屏幕、窗口、标签页或摄像头。浏览器在本机算画面变化，只挑<b>切场景</b>和<b>动作累积</b>的关键帧；
    你一开口或打字就<b>立刻</b>带上最新几帧去问，画面切换时它也会按节奏主动说一句。图片只进当次请求，历史只留文字日记，<b>越看越贵</b>的问题不存在；
    结束后整场观看会沉淀成一颗星。${e.ready ? "" : "<br><b style='color:var(--danger)'>先到「设置」里填好模型的地址、Key 和模型名。</b>"}`;
  $("#paceSel").value = c.pace || "normal"; $("#ttsChk").checked = c.tts !== false;
}
async function renderDiary() {
  const list = await api("/api/eye/sessions?limit=50");
  $("#tab-diary").innerHTML = list.length ? list.map((s) => `<div class="sess" data-id="${s.id}"><div class="hhead"><b>「${esc(s.title)}」</b>
    <span class="t">${fmtDate(s.started_at)} · ${s.minutes} 分钟 · 对话 ${s.count.user + s.count.ai} 句${s.count.subtitle ? ` · 台词 ${s.count.subtitle}` : ""}</span>
    ${s.star_id ? `<span class="src">★ 星 #${s.star_id}</span>` : s.ended_at ? "" : '<span class="src s-undo">进行中</span>'}</div><div class="diary hidden"></div></div>`).join("")
    : `<div class="empty">还没有一起看过东西。</div>`;
  $$("#tab-diary .sess").forEach((el) => el.onclick = async () => {
    const box = el.querySelector(".diary"); if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
    const d = await api(`/api/eye/session/${el.dataset.id}`);
    const K = { user: "主人", ai: "我", subtitle: "台词", scene: "场景", note: "备注" };
    box.innerHTML = d.entries.filter((x) => x.text.replace(/[….\s]/g, "")).map((x) => `<div><span class="k">${K[x.kind]}${x.who ? "·" + esc(x.who) : ""}</span>${esc(x.text)}</div>`).join("") || "（只看没聊）";
    box.classList.remove("hidden");
  });
}
const F = (k, label, type = "text", extra = "") => { const v = CFG()[k]; return type === "checkbox"
  ? `<label class="chk"><input type="checkbox" data-k="${k}" ${v ? "checked" : ""}> ${label}</label>`
  : `<div><label>${label}</label><input data-k="${k}" type="${type}" value="${esc(v ?? "")}" ${extra}></div>`; };
const SEL = (k, label, opts) => `<div><label>${label}</label><select data-k="${k}">${opts.map(([v, t]) => `<option value="${v}" ${String(CFG()[k]) === v ? "selected" : ""}>${t}</option>`).join("")}</select></div>`;
function renderSettings() {
  const E = ST.engines, pre = ST.preflight || {};
  const card = (k) => { const e = E[k]; const key = `${e.engine}|${e.protocol}|${e.base_url}|${e.model}`; const p = pre[key];
    return `<div class="eng ${ST.engine === k ? "on" : ""}"><div class="hhead"><b>${esc(e.title)}</b>${ST.engine === k ? '<span class="src">当前</span>' : ""}
      <span class="acts"><button data-pre="${k}">能力预检</button></span></div>
      <div class="note">${esc(e.protocol === "anthropic" ? "Anthropic 兼容" : "OpenAI 兼容")} · ${esc(e.base_url || "未填地址")} · ${esc(e.model || "未填模型")} · Key ${e.api_key_set ? "已填" : "未填"}</div>
      <div class="note">${p ? (p.ok ? `<span class="ok">✔ 能看图</span>（回答「${esc(p.reply)}」，首字 ${p.first_ms} ms）` : `<span class="bad">✘ ${esc(p.reason)}</span>`) : "还没预检：发一张纯红小图问颜色，答得出才算真能看图。"}</div></div>`; };
  $("#tab-settings").innerHTML = `<div class="engcards">${card("frame")}${card("qwen_omni")}</div>
  <div class="form">
    <h4>引擎</h4><div class="row">${SEL("engine", "使用哪个引擎", [["frame", "通用帧模式"], ["qwen_omni", "千问 Omni 视频接口"]])}</div>
    <h4>通用帧模式 <small class="note">地址留空 = 沿用「设置 → LLM 维护」的地址、Key 和模型</small></h4>
    <div class="row">${SEL("frame_protocol", "协议", [["openai", "OpenAI 兼容"], ["responses", "OpenAI Responses"], ["anthropic", "Anthropic 兼容"]])}${F("frame_base_url", "地址（如 https://api.openai.com/v1）")}
      ${F("frame_api_key", "API Key", "password")}${F("frame_model", "识图模型（如 gpt-4o-mini / qwen-vl-max）")}
      ${SEL("frame_compose", "帧怎么送", [["grid", "拼成一张分镜图（省钱）"], ["multi", "每帧一张图"]])}</div>
    <h4>千问 Qwen3.8-Omni-Flash 视频接口 <small class="note">预留：填上 Key 就能用</small></h4>
    <div class="row">${SEL("qwen_protocol", "协议", [["openai", "OpenAI 兼容"], ["anthropic", "Anthropic 兼容"]])}${F("qwen_api_key", "API Key", "password")}${F("qwen_model", "模型")}
      ${F("qwen_base_url_openai", "OpenAI 兼容地址")}${F("qwen_base_url_anthropic", "Anthropic 兼容地址")}
      ${SEL("qwen_video_mode", "帧序列怎么送（OpenAI 协议）", [["video", "按视频送（不认就自动改多图）"], ["images", "每帧一张图"]])}
      ${F("qwen_voice", "音色（直接出语音时）")}${F("qwen_audio_out", "让模型直接出语音（预留，未实测）", "checkbox")}</div>
    <h4>看与说</h4>
    <div class="row">${F("max_frames", "每次最多带几帧", "number", 'min="1" max="16"')}${F("frame_width", "单帧宽度（像素）", "number", 'min="160" max="1920"')}
      ${SEL("pace", "主动吐槽节奏", [["off", "不主动说话"], ["calm", "安静（30 秒）"], ["normal", "陪看（15 秒）"], ["chatty", "话痨（8 秒）"]])}
      ${F("reply_chars", "回答尽量不超过几个字", "number")}${F("history_turns", "带最近几轮文字对话", "number")}${F("recall_budget_tokens", "相关星星预算（tok）", "number")}
      ${F("daily_budget_tokens", "每日 token 预算（0 = 不限）", "number")}${F("daily_max_requests", "每日请求上限（0 = 不限）", "number")}
      ${F("star_on_end", "结束时沉淀成一颗星", "checkbox")}${F("save_frames", "保存画面到本机（默认关）", "checkbox")}</div>
    <h4>声音</h4>
    <div class="row">${F("tts", "朗读 AI 的话（浏览器）", "checkbox")}${SEL("stt_mode", "转写", [["browser", "只听主人说话（浏览器识别）"], ["api", "也转写电影声音（转写服务）"]])}
      ${F("stt_base_url", "转写服务地址（留空沿用 LLM 地址）")}${F("stt_api_key", "转写 Key（留空沿用）", "password")}${F("stt_model", "转写模型")}</div>
    ${voiceSettingsHtml()}
    <div class="toolbar"><button id="saveEye" class="primary">保存设置</button></div>
  </div>`;
  $$("[data-pre]").forEach((b) => b.onclick = () => safe(async () => {
    b.disabled = true; b.textContent = "预检中…";
    const r = await api("/api/eye/preflight", { method: "POST", body: { engine: b.dataset.pre } });
    toast(r.ok ? `能看图：回答「${r.reply}」` : r.reason, !r.ok); await loadStatus(); renderSettings();
  }));
  $("#saveEye").onclick = () => safe(async () => {
    const patch = {};
    $$("#tab-settings [data-k]").forEach((el) => { const k = el.dataset.k;
      patch[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value || 0) : el.value; });
    const vpatch = {};
    $$("#tab-settings [data-v]").forEach((el) => { const k = el.dataset.v;
      vpatch[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value || 0) : el.value; });
    await api("/api/settings", { method: "PUT", body: { eye: patch, voice: vpatch } });
    toast("已保存"); await loadStatus(); renderSettings();
  });
  bindVoiceSettings();
}

// ---------------- 绑定 ----------------
$$(".ctabs button").forEach((b) => b.onclick = () => {
  $$(".ctabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "diary") safe(renderDiary);
  if (b.dataset.tab === "settings") renderSettings();
  if (b.dataset.tab === "voices") safe(renderVoices);
});
$$("[data-src]").forEach((b) => b.onclick = () => startCapture(b.dataset.src));
$("#stopBtn").onclick = () => stopCapture(true);
$("#voiceBtn").onclick = () => safe(toggleVoice);
$("#liveBadge").onclick = () => stopCapture(true);
const send = () => { const t = $("#ask").value.trim(); if (!t) return; $("#ask").value = ""; ask(t, "user"); };
$("#askBtn").onclick = send;
$("#ask").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) send(); if (speaking()) stopSpeaking(); });
$("#micChk").onchange = (e) => (e.target.checked ? (S ? startMic() : toast("开始观看后就会开始听")) : stopMic());
$("#paceSel").onchange = (e) => safe(async () => { await api("/api/settings", { method: "PUT", body: { eye: { pace: e.target.value } } }); ST.settings.pace = e.target.value; });
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input,textarea,select")) return;
  if (e.key === "h" || e.key === "H") ask("你看现在这个画面怎么样？", "user");
  if (e.key === "Escape" && media) stopCapture(true);
});
window.addEventListener("beforeunload", () => { if (S) navigator.sendBeacon(`/api/eye/session/${S.id}/end`, new Blob(["{}"], { type: "application/json" })); });
setupRegion();
loadStatus().catch((e) => { $("#intro").innerHTML = `<b style="color:var(--danger)">${esc(e.message)}</b>`; $$("[data-src]").forEach((b) => (b.disabled = true)); });


// ================= 宇宙之声：声纹分人 + 三路线索认名字 =================
let VS = null;                       // /api/voice/status
let vCap = null, vMic = null, vQueue = 0, vRoster = null, vPoll = null;
const VC = () => (VS && VS.settings) || {};
const voiceOn = () => !!(VS && VS.enabled && VS.ready);
async function loadVoice() {
  try { VS = await api("/api/voice/status"); } catch { VS = null; }
  const on = VS && VS.enabled;
  $("#voiceBtn").classList.toggle("on", !!on);
  $("#voiceState").textContent = !VS ? "不可用" : on ? (VS.ready ? "开" : "待安装") : "关";
  if (on && !VS.ready) renderInstall(); else if (!S || !on) { if (!vCap) $("#cast").classList.add("hidden"); }
}
async function toggleVoice() {
  const want = !(VS && VS.enabled);
  await api("/api/settings", { method: "PUT", body: { voice: { enabled: want } } });
  await loadVoice();
  if (!want) { stopVoice(); $("#cast").classList.add("hidden"); if (S) startSubtitles(); toast("宇宙之声已关闭"); return; }
  if (!VS.ready) { renderInstall(); return; }
  toast("宇宙之声已打开：会分辨每个说话人");
  if (S && media) { stopSubtitles(); startVoice(S.source); }
}
function renderInstall() {
  const c = $("#cast"); c.classList.remove("hidden");
  const j = VS.jobs || {}; const running = Object.values(j).some((x) => x.state === "running");
  const depsOk = Object.values(VS.deps).every((d) => d.ok);
  const rows = Object.entries(VS.models).map(([k, m]) => { const jb = j["model:" + k] || {};
    const pct = m.installed ? 100 : jb.total ? Math.round((jb.done || 0) / jb.total * 100) : 0;
    return `<div>${m.installed ? "✔" : jb.state === "error" ? "✘" : "○"} ${esc(m.title)} <small>${(m.download_bytes / 1e6).toFixed(1)} MB</small>${jb.error ? ` <span class="bad">${esc(jb.error)}</span>` : ""}</div><div class="bar"><i style="width:${pct}%"></i></div>`; }).join("");
  const dj = j.deps || {};
  c.innerHTML = `<div class="install"><div class="chead">🌌 <b>宇宙之声需要先装声纹插件</b><span class="grow"></span></div>
    还需下载约 <b>${VS.need_mb} MB</b>：放在程序目录 <code>models/voice/</code>，不进灵魂包、不进备份。只存声纹向量，不存录音。
    <div>${depsOk ? "✔" : dj.state === "error" ? "✘" : "○"} 运行库 sherpa-onnx + numpy ${depsOk ? "" : "<small>约 32 MB</small>"}${dj.error ? ` <span class="bad">${esc(dj.error)}</span>` : ""}</div>
    ${rows}
    <div class="toolbar"><button id="vInstall" class="primary" ${running ? "disabled" : ""}>${running ? "下载中…" : "一键下载"}</button>
    <small class="note">国内网络慢可以在「设置 → 宇宙之声」填下载加速前缀和 pip 镜像。</small></div></div>`;
  $("#vInstall").onclick = () => safe(async () => { await api("/api/voice/install", { method: "POST", body: {} }); pollInstall(); });
  if (running) pollInstall();
}
function pollInstall() {
  clearInterval(vPoll);
  vPoll = setInterval(async () => {
    try { VS = await api("/api/voice/status"); } catch { return; }
    const running = Object.values(VS.jobs || {}).some((x) => x.state === "running");
    if (VS.ready) { clearInterval(vPoll); toast("声纹插件装好了"); await loadVoice(); if (S && media) { stopSubtitles(); startVoice(S.source); } else $("#cast").classList.add("hidden"); return; }
    renderInstall(); if (!running) clearInterval(vPoll);
  }, 1200);
}

// ---- 采集 16kHz PCM（ScriptProcessor 降采样；兼容所有主流浏览器） ----
function pcmCapture(stream, chunkSec, onChunk) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const mute = ctx.createGain(); mute.gain.value = 0;
  const ratio = ctx.sampleRate / 16000, need = Math.round(16000 * chunkSec);
  let buf = new Int16Array(need), n = 0, pos = 0, t0 = Date.now() / 1000;
  proc.onaudioprocess = (ev) => {
    const ch = ev.inputBuffer.getChannelData(0);
    for (; pos < ch.length; pos += ratio) {
      const v = Math.max(-1, Math.min(1, ch[Math.floor(pos)]));
      buf[n++] = v * 32767;
      if (n >= need) { const out = buf; buf = new Int16Array(need); n = 0; const t = t0; t0 = Date.now() / 1000; onChunk(out, t); }
    }
    pos -= ch.length;
  };
  src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
  return { stop: () => { try { proc.disconnect(); src.disconnect(); ctx.close(); } catch {} }, flush: () => { if (n > 16000) { const out = buf.slice(0, n); n = 0; onChunk(out, t0); } } };
}
const pcmRms = (a) => { let s = 0; for (let i = 0; i < a.length; i += 4) s += (a[i] / 32768) ** 2; return Math.sqrt(s / (a.length / 4)); };
async function startVoice(src) {
  if (!S || vCap) return;
  let tracks = media ? media.getAudioTracks() : [];
  if (src === "camera") {
    try { vMic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); tracks = vMic.getAudioTracks(); }
    catch (e) { sys("宇宙之声没拿到麦克风：" + (e.message || e.name)); return; }
  }
  if (!tracks.length) { sys("🌌 这次共享没有带声音，宇宙之声听不到。请选「标签页」并勾选「共享音频」。"); return; }
  const sec = Math.max(3, Math.min(20, +VC().chunk_sec || 8));
  vCap = pcmCapture(new MediaStream(tracks), sec, sendVoice);
  $("#cast").classList.remove("hidden");
  renderCast(await api(`/api/voice/session/${S.id}/speakers`).catch(() => null));
  sys(`🌌 宇宙之声开始听：每 ${sec} 秒分一次人。${VS.stt_ready ? "台词会逐句转写并标上说话人。" : "还没配置转写服务，只分人不转写（设置 → 声音 → 也转写电影声音）。"}`);
}
function stopVoice() { if (vCap) { vCap.flush(); vCap.stop(); } vCap = null; if (vMic) vMic.getTracks().forEach((t) => t.stop()); vMic = null; }
async function sendVoice(pcm, t0) {
  if (!S || pcmRms(pcm) < 0.004) return;            // 静音段不送，省 CPU
  if (vQueue >= 2) return;                           // 后端处理不过来就丢一段，保证实时
  vQueue++;
  const sid = S.id;
  try {
    const r = await fetch(`/api/voice/session/${sid}/audio?sr=16000&t0=${t0.toFixed(2)}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: pcm.buffer });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { $("#stat").textContent = "🌌 " + (d.detail || `HTTP ${r.status}`); return; }
    for (const l of d.lines) if (l.text) { const el = addMsg("sub", ""); el.querySelector(".b").innerHTML = `<span class="who" style="color:${l.color}">${esc(l.display)}</span>${esc(l.text)}`; }
    if (d.new_speakers.length) $("#stat").textContent = `🌌 新面孔：${d.new_speakers.join("、")}`;
    if (d.merges.length) sys(`🌌 重新聚类：${d.merges.map((m) => `${m.drop_label} 其实就是 ${m.keep_label}`).join("；")}`);
    onNames(d.names, false);
    const last = d.lines[d.lines.length - 1];
    renderCast(d.roster, last && last.speaker, d);
  } catch (e) { $("#stat").textContent = "🌌 " + e.message; } finally { vQueue--; }
}
function onNames(changes, refresh = true) {
  const SRC = { visual: "画面", dialogue: "台词", self_intro: "自我介绍", owner: "主人" };
  for (const c of changes || []) {
    if (c.status === "confirmed") sys(`🌌 认出来了：${c.label} = ${c.name}（${SRC[c.source] || c.source}）`);
    else if (c.status === "proposed") $("#stat").textContent = `🌌 ${c.label} 可能叫 ${c.name}（${SRC[c.source] || c.source}）`;
  }
  if (refresh && changes && changes.length && S) api(`/api/voice/session/${S.id}/speakers`).then((r) => renderCast(r)).catch(() => {});
}
function renderCast(roster, talking, info) {
  if (!roster) return; vRoster = roster;
  const c = $("#cast"); c.classList.remove("hidden");
  const fmtS = (x) => (x >= 60 ? `${Math.round(x / 60)} 分` : `${Math.round(x)} 秒`);
  c.innerHTML = `<div class="chead">🌌 <b>片中的人</b><span class="note">${roster.speakers.length} 位</span><span class="grow"></span>
      <button id="vIdentify" title="让 AI 读带说话人的台词，从称呼里推断名字">让 AI 认人</button><button id="vRecluster" title="把被拆开的同一个人合并">重新聚类</button></div>
    <div class="now" id="vNow">${talking ? `正在说话：${esc((roster.speakers.find((s) => s.id === talking) || {}).display || "")}` : ""}${info ? ` · 分人耗时 ${info.proc_ms} ms / ${info.audio_sec} 秒音频` : ""}</div>
    ${roster.speakers.map((s) => { const p = (s.proposals || []).filter((x) => !x.rejected).sort((a, b) => b.votes - a.votes)[0];
      return `<div class="spk ${s.id === talking ? "talking" : ""}" data-id="${s.id}"><span class="dot" style="background:${s.color};color:${s.color}"></span>
        <span class="nm">${esc(s.name_status === "confirmed" ? s.name : s.label)}</span>
        ${s.name_status === "confirmed" && !s.owner ? `<span class="lb">${esc(s.label)}</span><span class="st-confirmed">✔</span>` : ""}
        ${s.name_status !== "confirmed" && p ? `<span class="pp">可能是 ${esc(p.name)}（${p.votes} 票）</span><button data-act="confirm">✓</button><button data-act="reject" data-name="${esc(p.name)}">✗</button>` : ""}
        <span class="meta">${s.gender || ""}${s.f0 ? ` ${Math.round(s.f0)}Hz` : ""} · ${fmtS(s.seconds)}</span>
        ${s.owner ? "" : `<button data-act="rename" title="改名">✏</button><button data-act="merge" title="和另一个人合并">⇄</button>`}</div>`; }).join("") || '<div class="note">还没听到人说话。</div>'}`;
  $("#vIdentify").onclick = () => safe(async () => { const r = await api(`/api/voice/session/${S.id}/identify`, { method: "POST" }); onNames(r.changes, false); renderCast(r); if (!r.changes.length) toast("台词里还看不出名字"); });
  $("#vRecluster").onclick = () => safe(async () => { const r = await api(`/api/voice/session/${S.id}/recluster`, { method: "POST" }); toast(r.merges.length ? `合并了 ${r.merges.length} 对` : "没有需要合并的"); renderCast(r); });
  $$("#cast .spk button").forEach((b) => b.onclick = () => safe(async () => {
    const id = b.closest(".spk").dataset.id, act = b.dataset.act, base = `/api/voice/session/${S.id}`;
    let r;
    if (act === "rename") { const n = prompt("这个人叫什么？（留空 = 清除名字）"); if (n === null) return; r = await api(`${base}/speakers/${id}`, { method: "POST", body: { action: "set", name: n } }); }
    else if (act === "confirm") r = await api(`${base}/speakers/${id}`, { method: "POST", body: { action: "confirm" } });
    else if (act === "reject") r = await api(`${base}/speakers/${id}`, { method: "POST", body: { action: "reject", name: b.dataset.name } });
    else if (act === "merge") { const t = prompt("和谁是同一个人？输入对方的编号或名字（例如 男2）"); if (!t) return;
      const other = vRoster.speakers.find((s) => s.label === t.trim() || s.name === t.trim()); if (!other) throw new Error("找不到 " + t);
      r = await api(`${base}/merge`, { method: "POST", body: { keep: other.id, drop: id } }); }
    renderCast(r);
  }));
}

// ---- 声纹库 & 设置 ----
async function renderVoices() {
  const lib = await api("/api/voice/library");
  $("#tab-voices").innerHTML = `<div class="form vlib"><h4>主人的声音</h4>
    <div>${lib.owner.enrolled ? `✔ 已录入（${lib.owner.seconds} 秒，${fmtDate(lib.owner.enrolled_at)}）。用摄像头 + 麦克风一起看时，能分清哪句是你说的。` : "还没录入。录一段你的声音（念 8 秒左右），用麦克风一起看时就能分清哪句是你说的。"}</div>
    <div class="toolbar"><button id="vEnroll" class="primary">🎙 ${lib.owner.enrolled ? "重新录" : "录入"}我的声音（8 秒）</button>${lib.owner.enrolled ? '<button id="vOwnerDel" class="danger">删除</button>' : ""}</div>
    <h4>认识的人（按片名 / 系列）</h4>
    ${lib.titles.length ? lib.titles.map((t) => `<div class="sess"><div class="hhead"><b>「${esc(t.title)}」</b><span class="t">${t.speakers.map((s) => `${esc(s.name)}${s.gender ? `（${s.gender}）` : ""}`).join("、")}</span>
      <span class="acts"><button data-del="${esc(t.key)}" class="danger">删除</button></span></div></div>`).join("") : '<div class="empty">还没有。看片时确认过名字的人，结束后会存在这里；下次看同一部或同系列（第几集不同也算）会直接认出。</div>'}
    <div class="note">只存声纹向量（192 个数字）和名字，不存任何录音。</div></div>`;
  $$("[data-del]").forEach((b) => b.onclick = () => safe(async () => { if (!confirm("删除这部片的声纹？")) return; await api(`/api/voice/library/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); renderVoices(); }));
  if ($("#vOwnerDel")) $("#vOwnerDel").onclick = () => safe(async () => { await api("/api/voice/owner", { method: "DELETE" }); renderVoices(); });
  $("#vEnroll").onclick = () => safe(async () => {
    if (!voiceOn()) throw new Error("先打开宇宙之声并装好插件");
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const b = $("#vEnroll"); b.disabled = true; let left = 8; b.textContent = `请随便说点什么… ${left}`;
    const tick = setInterval(() => { left--; b.textContent = `请随便说点什么… ${left}`; }, 1000);
    await new Promise((ok) => { const cap = pcmCapture(mic, 8, async (pcm) => { cap.stop(); mic.getTracks().forEach((t) => t.stop()); clearInterval(tick);
      try { await api("/api/voice/owner/enroll?sr=16000", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: pcm.buffer }); toast("录好了"); } catch (e) { toast(e.message, true); }
      ok(); }); });
    renderVoices();
  });
}
const VF = (k, label, type = "text", extra = "") => { const v = VC()[k]; return type === "checkbox"
  ? `<label class="chk"><input type="checkbox" data-v="${k}" ${v ? "checked" : ""}> ${label}</label>`
  : `<div><label>${label}</label><input data-v="${k}" type="${type}" value="${esc(v ?? "")}" ${extra}></div>`; };
function voiceSettingsHtml() {
  if (!VS) return "";
  return `<h4>宇宙之声（声纹插件） <small class="note">${VS.ready ? `已安装 · ${esc(VS.models_dir)}` : `未安装，打开开关时一键下载约 ${VS.need_mb} MB`}</small></h4>
    <div class="row">${VF("enabled", "打开宇宙之声", "checkbox")}${VF("chunk_sec", "每隔几秒分一次人", "number", 'min="3" max="20"')}
      ${VF("match_threshold", "同一人的声纹相似度", "number", 'step="0.01" min="0.2" max="0.9"')}${VF("merge_threshold", "重新聚类时合并的相似度", "number", 'step="0.01" min="0.3" max="0.95"')}
      ${VF("min_learn_sec", "只用多长以上的句子学声纹（秒）", "number", 'step="0.1"')}${VF("gender_split_hz", "男女分界音高（Hz）", "number")}
      ${VF("auto_identify_every", "每多少句台词让 AI 认一次名字（0 = 手动）", "number")}${VF("auto_accept_votes", "几票自动确认名字（0 = 都等我确认）", "number")}
      ${VF("visual_naming", "提问时让视觉模型顺便认人", "checkbox")}${VF("transcribe", "逐句转写并标上说话人", "checkbox")}
      ${VF("use_library", "按片名 / 系列记住认识的人", "checkbox")}${VF("allow_override", "允许自动线索改掉已确认的名字", "checkbox")}
      ${VF("download_mirror", "模型下载加速前缀（可空）")}${VF("pip_index", "pip 镜像（可空）")}</div>
    ${VS.ready ? '<div class="toolbar"><button id="vRemove" class="danger">卸载声纹模型（释放空间）</button></div>' : ""}`;
}
function bindVoiceSettings() {
  if ($("#vRemove")) $("#vRemove").onclick = () => safe(async () => { if (!confirm("删除已下载的声纹模型？声纹库不受影响。")) return; await api("/api/voice/models", { method: "DELETE" }); await loadVoice(); renderSettings(); });
}

// 桌面版心跳：页面全部关闭后，后台服务才会自动退出（仅浏览器兜底模式用到）
setInterval(() => fetch("/api/ping", { method: "POST" }).catch(() => {}), 15000); fetch("/api/ping", { method: "POST" }).catch(() => {});
