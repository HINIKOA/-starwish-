const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

export function offlineGraphHtml({ target, status, filter, relations, svg }) {
  // The SVG is built by the live renderer using fixed tags and textContent.
  return `<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>星愿 · 代码关系快照</title>
<style>
  :root{color-scheme:dark;background:#101419;color:#e8ecf1;font:15px/1.6 sans-serif}
  body{margin:0} main{max-width:920px;margin:auto;padding:24px}
  h1{margin:0;color:#b8defc} p{overflow-wrap:anywhere}
  section{overflow:auto;border:1px solid #47536a;border-radius:8px;background:#181f29}
  svg{display:block} rect{fill:#242d3a;stroke:#98aabb} text{fill:#f0f3f8;font:13px monospace}
  .gf-map-node-meta{fill:#acb9ca;font-size:11px} [data-node-kind="file"] rect{stroke:#87c9ff}
  [data-node-kind="doc"] rect{stroke-dasharray:4 3}
  [data-kind]{fill:none;stroke:#a4b5c8;stroke-width:1.7}
  [data-kind="imports"]{stroke:#87c9ff;stroke-dasharray:7 3}
  [data-kind="calls"]{stroke:#95dabb}
  [data-kind="doc-links-code"]{stroke-dasharray:2 4}
  [data-kind="co-edit"]{stroke:#f1bf7b;stroke-dasharray:9 4 2 4}
  pre{white-space:pre-wrap;overflow-wrap:anywhere} details{margin-top:18px}
</style>
<main>
  <h1>代码关系快照</h1>
  <p>目标：${escape(target || "项目概览")} · 筛选：${escape(filter || "全部关系")}</p>
  <p>${escape(status)}</p>
  <p>仅供离线查看。文件与符号名称可能包含项目内部信息。</p>
  <section tabindex="0" aria-label="代码关系图，可滚动">${svg}</section>
  <details open><summary>文字关系表</summary><pre>${escape(relations)}</pre></details>
</main>
</html>`;
}

export function downloadGraphSnapshot(svgElement, metadata) {
  const snapshot = svgElement.cloneNode(true);
  snapshot.setAttribute("role", "img");
  for (const node of snapshot.querySelectorAll("[tabindex]")) {
    node.removeAttribute("tabindex");
    node.removeAttribute("role");
    node.removeAttribute("aria-pressed");
  }
  const svg = new XMLSerializer().serializeToString(snapshot);
  const blob = new Blob([offlineGraphHtml({ ...metadata, svg })], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "starwish-code-graph.html";
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
