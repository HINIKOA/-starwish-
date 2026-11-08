// 星愿记忆 —— 外观（v0.11.0）：主题（浅色 / 深色 / 系统）、主题颜色、缩放。所有页面在 <head> 里同步加载，先用本地缓存防闪烁，再向服务端校正。
(function () {
  const KEY = "starwish.ui";
  const DEF = { theme: "dark", accent: "#00B96B", scale: 100, chat_side: "right", task_side: "left" };
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let cur = DEF;
  function hexToRgb(h) { const m = /^#?([0-9a-f]{6})$/i.exec(h || ""); if (!m) return [0, 185, 107]; const n = parseInt(m[1], 16); return [n >> 16, (n >> 8) & 255, n & 255]; }
  function linear(channel) { const s = channel / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; }
  function apply(ui) {
    cur = Object.assign({}, DEF, cur, ui || {});
    const root = document.documentElement;
    const dark = cur.theme === "dark" || (cur.theme === "system" && mq && mq.matches);
    root.dataset.theme = dark ? "dark" : "light";
    root.dataset.themePref = cur.theme;
    const [r, g, b] = hexToRgb(cur.accent);
    root.style.setProperty("--brand", `rgb(${r},${g},${b})`);
    root.style.setProperty("--brand-rgb", `${r},${g},${b}`);
    root.style.setProperty("--brand-soft", `rgba(${r},${g},${b},${dark ? 0.18 : 0.12})`);
    const luminance = 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
    root.style.setProperty("--brand-on", luminance > 0.179 ? "#000" : "#fff");
    const s = Math.max(50, Math.min(200, Number(cur.scale) || 100));
    root.style.zoom = s === 100 ? "" : String(s / 100);   // 缩放：zoom（Chromium / WebView2 支持）
    root.dataset.chatSide = cur.chat_side || "right";
    root.dataset.taskSide = cur.task_side || "left";
    try { localStorage.setItem(KEY, JSON.stringify(cur)); } catch (e) { /* 忽略 */ }
    window.dispatchEvent(new CustomEvent("starwish-ui", { detail: cur }));
  }
  try { const c = JSON.parse(localStorage.getItem(KEY) || "null"); if (c) cur = Object.assign({}, DEF, c); } catch (e) { /* 忽略 */ }
  apply(cur);
  if (mq && mq.addEventListener) mq.addEventListener("change", () => cur.theme === "system" && apply(cur));
  fetch("/api/settings").then((r) => r.json()).then((c) => c && c.ui && apply(c.ui)).catch(() => {});
  window.StarTheme = { apply, get: () => cur };
})();
