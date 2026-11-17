import { downloadGraphSnapshot } from "./graphflow-export.mjs";

const byId = (id) => document.getElementById(id);
const form = byId("gf-form");
const submit = byId("gf-submit");
const status = byId("gf-status");
const reason = byId("gf-reason");
const error = byId("gf-error");
const result = byId("gf-result");
const resultStatus = byId("gf-result-status");
const code = byId("gf-code");
const placeholder = byId("gf-placeholder");
let available = false;
let busy = false;
const edit = byId("gf-edit-form");
const editSubmit = byId("gf-edit-submit");
const editCheck = byId("gf-edit-check");
const editDiff = byId("gf-edit-diff");
const editStatus = byId("gf-edit-status");
const editError = byId("gf-edit-error");
let editAvailable = false;
let editBusy = false;
let revision = 0;
let previewId = "";

async function request(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const message = Array.isArray(detail)
      ? detail.map((entry) => entry?.msg).filter(Boolean).join("；")
      : typeof detail === "string" ? detail : "";
    throw new Error(message || `服务请求失败（HTTP ${response.status}）`);
  }
  if (!payload || typeof payload !== "object") throw new Error("服务返回了无效的数据。");
  return payload;
}

function showError(message = "") {
  error.textContent = message;
  error.hidden = !message;
}

function setAvailability(state) {
  available = state.available === true;
  status.dataset.state = available ? "ready" : "unavailable";
  status.textContent = available ? "引擎可用" : "引擎不可用";
  byId("gf-workspace").textContent = state.workspace || "未配置";
  byId("gf-provider").textContent = state.provider || "—";
  reason.textContent = state.reason || "";
  reason.hidden = !state.reason;
  submit.disabled = !available;
  editAvailable = Boolean(state.workspace);
  updateEditButtons();
  const setup = (section, form, hint, enabled, disabledMessage) => {
    byId(section).hidden = !state.workspace;
    const ready = enabled && state.operator_key_configured === true;
    byId(form).hidden = !ready;
    byId(hint).textContent = !enabled ? disabledMessage
      : !ready ? "网页服务尚未设置至少 24 位的审批密钥。" : "";
    byId(hint).hidden = ready;
  };
  setup("gf-approval", "gf-approval-form", "gf-approval-setup", state.edit_queue === true,
    "网页服务尚未启用编辑审批。若要接收桌面 MCP 的预览，桌面 MCP 也需启用同一审批队列。");
  setup("gf-test", "gf-test-form", "gf-test-setup", state.test_enabled === true,
    "网页服务尚未配置预设测试。");
  byId("gf-workflow").hidden = !state.workspace;
  byId("gf-planner").hidden = !state.workspace;
  byId("gf-artifact").hidden = !available;
  byId("gf-dependency").hidden = !available;
  byId("gf-tasks").hidden = byId("gf-task-save").hidden = state.tasks_enabled !== true || !state.workspace;
  byId("gf-legacy-tasks").hidden = state.legacy_tasks_enabled !== true || !state.workspace;
  byId("gf-test-task-row").hidden = state.tasks_enabled !== true || state.test_enabled !== true
    || state.operator_key_configured !== true;
}

const artifactInput = byId("gf-artifact-data");
const graphForm = byId("gf-dependency-form");
const graphTarget = byId("gf-dependency-target");
const graphFilter = byId("gf-dependency-kind");
const graphMap = byId("gf-dependency-map");
const graphMessage = byId("gf-dependency-status");
const graphNeighbors = byId("gf-dependency-neighborhood");
const graphSvg = byId("gf-dependency-svg");
const graphKinds = { defines: "定义", imports: "导入", calls: "调用", "doc-links-code": "文档关联", "co-edit": "历史共改" };
let currentGraph = null;
let selectedNode = -1;

function svgNode(tag, attributes) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function graphName(node) {
  return node.symbol ? `${node.file}:${node.line} · ${node.symbol}` : node.file;
}

function selectGraphNode(index) {
  selectedNode = index;
  const node = currentGraph.nodes[index];
  graphNeighbors.disabled = !node || node.file === currentGraph.target;
  byId("gf-dependency-selection").textContent = node ? graphName(node) : "选择节点查看文件位置和相邻关系。";
  for (const element of graphSvg.querySelectorAll("[data-node]")) {
    element.setAttribute("aria-pressed", String(Number(element.dataset.node) === index));
  }
  for (const element of graphSvg.querySelectorAll("[data-kind]")) {
    element.classList.toggle("gf-map-edge-active", index === -1 ||
      Number(element.dataset.source) === index || Number(element.dataset.target) === index);
  }
}

function renderDependencyGraph() {
  const { nodes, edges } = currentGraph;
  const visible = edges.filter((edge) => !graphFilter.value || edge.kind === graphFilter.value);
  const lanes = [0, 0];
  const positions = nodes.map((node, index) => {
    const lane = currentGraph.target ? Number(node.file !== currentGraph.target) : index % 2;
    return { x: lane ? 448 : 18, y: 24 + lanes[lane]++ * 56, lane };
  });
  const height = Math.max(130, 26 + 56 * Math.max(...lanes));
  graphSvg.setAttribute("viewBox", `0 0 720 ${height}`);
  graphSvg.setAttribute("width", 720);
  graphSvg.setAttribute("height", height);
  const defs = svgNode("defs", {});
  const arrow = svgNode("marker", { id: "gf-map-arrow", viewBox: "0 0 8 8", refX: 7, refY: 4,
    markerWidth: 7, markerHeight: 7, orient: "auto" });
  arrow.append(svgNode("path", { d: "M0 0 L8 4 L0 8 Z", fill: "context-stroke" }));
  defs.append(arrow);
  const links = visible.map((edge) => {
    const a = positions[edge.source], b = positions[edge.target];
    const x1 = a.lane === 0 ? a.x + 254 : a.x;
    const x2 = b.lane === 0 ? b.x + 254 : b.x;
    const y1 = a.y + 16, y2 = b.y + 25;
    const bend = a.lane === b.lane ? x1 + (a.lane === 0 ? 45 : -45) : 360;
    const line = svgNode("path", { d: `M${x1} ${y1} C${bend} ${y1} ${bend} ${y2} ${x2} ${y2}`,
      "data-kind": edge.kind, "data-source": edge.source, "data-target": edge.target,
      "marker-end": "url(#gf-map-arrow)" });
    const label = svgNode("title", {});
    label.textContent = graphKinds[edge.kind];
    line.append(label);
    return line;
  });
  const points = nodes.map((node, index) => {
    const { x, y } = positions[index];
    const group = svgNode("g", { transform: `translate(${x} ${y})`, role: "button", tabindex: 0,
      "aria-label": graphName(node), "aria-pressed": false, "data-node": index, "data-node-kind": node.kind });
    const title = svgNode("title", {});
    title.textContent = graphName(node);
    const label = svgNode("text", { x: 12, y: 17 });
    const file = node.file.split("/").at(-1);
    const name = node.symbol || file;
    label.textContent = name.length > 30 ? `${name.slice(0, 29)}…` : name;
    const type = svgNode("text", { x: 12, y: 33, class: "gf-map-node-meta" });
    type.textContent = node.symbol ? `${file.slice(0, 26)}:${node.line}` : node.kind === "doc" ? "文档" : "源码文件";
    group.append(title, svgNode("rect", { width: 254, height: 41, rx: 7 }), label, type);
    group.addEventListener("click", () => selectGraphNode(index));
    group.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectGraphNode(index);
    });
    return group;
  });
  graphSvg.replaceChildren(defs, ...links, ...points);
  const lines = visible.map((edge) => {
    const detail = edge.kind === "co-edit" ? `（${edge.co_commit_count} 次，相关度 ${Math.round(edge.confidence * 100)}%）` : "";
    return `${graphName(nodes[edge.source])} → ${graphKinds[edge.kind]}${detail} → ${graphName(nodes[edge.target])}`;
  });
  byId("gf-dependency-result").textContent = [...nodes.map(graphName), "", ...lines].join("\n");
  graphMessage.textContent = `显示 ${nodes.length} 个节点、${lines.length} 条关系；静态分析可能遗漏。`
    + (currentGraph.truncated || currentGraph.index?.partial || currentGraph.history?.more
        ? " 当前为部分结果，建议指定文件缩小范围。" : "")
    + (currentGraph.history?.status?.startsWith("unavailable_") ? " 本次无法读取共改历史。" : "");
  selectGraphNode(selectedNode);
}

graphFilter.addEventListener("change", renderDependencyGraph);
byId("gf-dependency-export").addEventListener("click", () => {
  if (!currentGraph || graphMap.hidden) return;
  downloadGraphSnapshot(graphSvg, {
    target: currentGraph.target,
    status: graphMessage.textContent,
    filter: graphKinds[graphFilter.value],
    relations: byId("gf-dependency-result").textContent,
  });
});
graphNeighbors.addEventListener("click", () => {
  const node = currentGraph?.nodes[selectedNode];
  if (!node) return;
  graphTarget.value = node.file;
  graphForm.requestSubmit();
});
graphForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("gf-dependency-load");
  if (!available || button.disabled) return;
  const restoreFocus = document.activeElement === graphNeighbors || graphSvg.contains(document.activeElement);
  graphMap.hidden = true;
  graphMessage.textContent = "正在构建代码关系…";
  button.disabled = true;
  try {
    const graph = await request("/api/graphflow/graph", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: graphTarget.value.trim() }),
    });
    currentGraph = graph;
    selectedNode = -1;
    graphFilter.value = "";
    renderDependencyGraph();
    graphMap.hidden = !graph.nodes.length;
  } catch (failure) {
    graphMessage.textContent = failure.message;
  } finally {
    button.disabled = false;
    if (restoreFocus) button.focus();
  }
});

const artifactStatus = byId("gf-artifact-status");
const artifactButtons = [byId("gf-artifact-export"), byId("gf-artifact-import")];
for (const [index, button] of artifactButtons.entries()) {
  button.addEventListener("click", async () => {
    const mode = index === 0 ? "export" : "import";
    for (const action of artifactButtons) action.disabled = true;
    artifactStatus.textContent = "正在处理索引清单…";
    try {
      const reply = await request("/api/graphflow/artifact", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, artifact: mode === "import" ? artifactInput.value.trim() : "" }),
      });
      if (mode === "export") artifactInput.value = reply.artifact;
      artifactStatus.textContent = mode === "export" ? `已生成 ${reply.file_count} 个文件的清单。` :
        `已校验 ${reply.file_count} 个文件；本次预热 ${reply.warmed} 个，未解析 ${reply.unparsed} 个。`;
    } catch (failure) {
      artifactStatus.textContent = failure.message;
    } finally {
      for (const action of artifactButtons) action.disabled = false;
    }
  });
}

function formatImpact(payload) {
  if (!Array.isArray(payload.impacts)) throw new Error("服务未返回影响分析结果。");
  const lines = ["静态分析：调用点为候选；未确认重导出不代表没有依赖。"];
  const resolutions = { direct_module: "直接指向目标模块", reexport_module: "经命名重导出指向目标",
    other_module: "指向其他模块", unresolved: "未解析" };
  for (const item of payload.impacts) {
    lines.push("", `${payload.file}:${item.line}–${item.end_line} · ${item.symbol}`);
    lines.push(`调用候选：${item.total_call_sites}（显示 ${item.call_sites.length} 条）`);
    for (const site of item.call_sites) {
      lines.push(`  ${site.file}:${site.line}${site.caller ? ` · ${site.caller}` : ""}`);
    }
    lines.push(`导入候选：${item.total_importers ?? item.importers.length}（显示 ${item.importers.length} 条）`);
    for (const site of item.importers) {
      const kind = resolutions[site.resolution];
      const detail = kind ? ` · ${kind}${site.resolved_file ? `：${site.resolved_file}` : ""}` : "";
      const chain = site.reexport_chain?.map(({ file, line }) => `${file}:${line}`).join(" → ");
      lines.push(`  ${site.file}:${site.line}${detail}${chain ? `（${chain}）` : ""}`);
    }
  }
  if (payload.truncated) lines.push("", "匹配符号较多，仅显示前 6 项。");
  if (payload.index?.partial) lines.push("", "索引范围受容量限制，结果可能不完整。");
  return lines.join("\n");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!available || busy) return;
  const query = byId("gf-query").value.trim();
  const target = byId("gf-target").value.trim();
  const impact = byId("gf-impact").checked;
  const compact = byId("gf-context").checked;
  if (!query && !target) {
    showError("请输入关键词、文件或符号。");
    byId("gf-query").focus();
    return;
  }
  if (query && target) {
    showError("关键词和文件或符号只能填写一项。");
    byId("gf-query").focus();
    return;
  }
  if (impact && !target.includes("#")) {
    showError("影响分析请填写文件#符号，并留空关键词。");
    byId("gf-target").focus();
    return;
  }
  if (compact && (!query || impact || byId("gf-skeleton").checked)) {
    showError("上下文查询只用关键词，请取消其他查询模式。");
    return;
  }

  showError();
  busy = true;
  submit.disabled = true;
  submit.textContent = "查询中…";
  code.textContent = "";
  code.hidden = true;
  placeholder.hidden = false;
  placeholder.textContent = "正在检索已配置的工作区…";
  resultStatus.textContent = "";
  result.setAttribute("aria-busy", "true");
  try {
    const payload = await request(compact ? "/api/graphflow/context" : impact ? "/api/graphflow/impact" : "/api/graphflow/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(compact ? { query } : impact ? { target } : { query, target, skeleton: byId("gf-skeleton").checked }),
    });
    const text = compact ? JSON.stringify(payload, null, 2) : impact ? formatImpact(payload) : payload.text;
    if (typeof text !== "string") throw new Error("服务未返回查询正文。");
    if (text.trim()) {
      code.textContent = text;
      code.hidden = false;
      placeholder.hidden = true;
      resultStatus.textContent = "查询完成";
    } else {
      placeholder.textContent = "没有找到匹配结果。可换一个关键词或检查文件路径。";
      resultStatus.textContent = "未找到匹配";
    }
  } catch (failure) {
    showError(failure.message);
    placeholder.textContent = "未能获取查询结果。";
    resultStatus.textContent = "查询失败";
  } finally {
    busy = false;
    submit.disabled = !available;
    submit.textContent = "查询代码";
    result.removeAttribute("aria-busy");
  }
});

function updateEditButtons() {
  editSubmit.disabled = !editAvailable || editBusy;
  editCheck.disabled = !previewId || editBusy;
}

function showEditError(message = "") {
  editError.textContent = message;
  editError.hidden = !message;
}

function resetPreview() {
  previewId = "";
  editDiff.hidden = true;
  editDiff.textContent = "";
  editStatus.textContent = "";
  showEditError();
}

edit.addEventListener("input", () => {
  revision += 1;
  resetPreview();
  updateEditButtons();
});

function editRequest(action, body) {
  return request(`/api/graphflow/edit/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

edit.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editAvailable || editBusy) return;
  resetPreview();
  const current = revision;
  editBusy = true;
  updateEditButtons();
  editStatus.textContent = "正在生成预览…";
  try {
    const payload = await editRequest("preview", {
      path: byId("gf-edit-path").value,
      start_line: byId("gf-edit-start").valueAsNumber,
      end_line: byId("gf-edit-end").valueAsNumber,
      expected: byId("gf-edit-expected").value,
      replacement: byId("gf-edit-replacement").value,
    });
    if (current !== revision) return;
    previewId = payload.preview_id;
    editDiff.textContent = payload.diff;
    editDiff.hidden = false;
    editStatus.textContent = "预览已生成，有效期 10 分钟。未写入文件。";
  } catch (failure) {
    if (current === revision) {
      editStatus.textContent = "";
      showEditError(failure.message);
    }
  } finally {
    editBusy = false;
    updateEditButtons();
  }
});

editCheck.addEventListener("click", async () => {
  if (!previewId || editBusy) return;
  const current = revision;
  editBusy = true;
  showEditError();
  updateEditButtons();
  try {
    await editRequest("check", { preview_id: previewId });
    if (current === revision) editStatus.textContent = "原文件未变化，预览仍有效。";
  } catch (failure) {
    if (current === revision) {
      resetPreview();
      showEditError(failure.message);
    }
  } finally {
    editBusy = false;
    updateEditButtons();
  }
});

const queueItems = byId("gf-queue-items");
const queueDiff = byId("gf-queue-diff");
const queueStatus = byId("gf-queue-status");
const queueActions = byId("gf-queue-actions");
const queueMutate = byId("gf-queue-mutate");
const operatorKey = byId("gf-operator-key");
const queueLoad = byId("gf-queue-load");
const stateLabel = {
  pending: "待审批", applying: "写入待确认", applied: "已写入", rolling_back: "回滚待确认",
  recovery_needed: "批量操作待恢复",
  rolled_back: "已回滚", stale: "原文已变化", expired: "已过期", archived: "已归档", failed: "写入结果未确认",
};
let selected = null;
let queueBusy = false;

function queueRequest(action, body) {
  return request(`/api/graphflow/edit/${action}`, {
    method: body ? "POST" : "GET",
    headers: { "X-StarWish-Operator-Key": operatorKey.value, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
}

function setQueueBusy(value) {
  queueBusy = value;
  operatorKey.disabled = value;
  queueLoad.disabled = value;
  queueMutate.disabled = value;
}

function actionFor(item) {
  return { pending: "apply", applied: "rollback", recovery_needed: "rollback", applying: "recover", rolling_back: "recover" }[item?.state];
}

function selectItem(item) {
  selected = item;
  queueDiff.textContent = item?.diff || "";
  queueDiff.hidden = !item?.diff;
  const action = actionFor(item);
  queueActions.hidden = !action;
  queueMutate.textContent = { apply: "确认写入…", rollback: "确认回滚…", recover: "核对中断状态" }[action] || "";
  for (const node of queueItems.children) {
    node.setAttribute("aria-pressed", String(node.dataset.id === item?.preview_id));
  }
}

function renderQueue(items) {
  selectItem(null);
  queueItems.replaceChildren();
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.id = item.preview_id;
    button.setAttribute("aria-pressed", "false");
    const label = item.paths ? `${item.paths.length} 个文件` : item.path;
    button.textContent = `${stateLabel[item.state] || item.state} · ${label} · ${item.preview_id.slice(0, 8)}`;
    button.addEventListener("click", () => { if (!queueBusy) selectItem(item); });
    queueItems.append(button);
  }
}

operatorKey.addEventListener("input", () => {
  renderQueue([]);
  queueStatus.textContent = "";
});

byId("gf-approval-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (queueBusy) return;
  setQueueBusy(true);
  try {
    const items = await queueRequest("queue");
    renderQueue(items);
    queueStatus.textContent = items.length ? `${items.length} 条记录，请选择一条核对。` : "没有审批记录。";
  } catch (failure) {
    renderQueue([]);
    queueStatus.textContent = failure.message;
  } finally {
    setQueueBusy(false);
  }
});

queueMutate.addEventListener("click", async () => {
  const item = selected;
  const action = actionFor(item);
  if (!action || queueBusy) return;
  const confirmed = action === "apply" ? item.result_sha256 : item.base_sha256;
  const verb = action === "apply" ? "写入选中的变更" : "恢复变更前的文件内容";
  if (action !== "recover" && !window.confirm(`确认${verb}？\n${(item.paths || [item.path]).join("\n")}\nSHA-256: ${confirmed}`)) return;
  setQueueBusy(true);
  try {
    const body = { preview_id: item.preview_id };
    if (action !== "recover") body.confirmed_sha256 = confirmed;
    const outcome = await queueRequest(action, body);
    renderQueue([]);
    const label = outcome.paths ? `${outcome.paths.length} 个文件` : outcome.path;
    queueStatus.textContent = `${stateLabel[outcome.state] || outcome.state} · ${label}。重新读取队列可查看最新状态。`;
  } catch (failure) {
    renderQueue([]);
    queueStatus.textContent = failure.message;
  } finally {
    setQueueBusy(false);
  }
});

const testKey = byId("gf-test-key");
const testStatus = byId("gf-test-status");
const testItems = byId("gf-test-items");
const testPlan = byId("gf-test-plan");
const testRun = byId("gf-test-run");
const testRecords = byId("gf-test-records");
const testLog = byId("gf-test-log");
const testNext = byId("gf-test-next");
const testLoad = byId("gf-test-load");
const testTask = byId("gf-test-task");
const testLinkStatus = byId("gf-test-link-status");
const testStates = {
  passed: "通过", failed: "失败", timed_out: "超时", running: "运行中",
  interrupted: "中断", launch_failed: "未能启动",
};
let profile = null;
let logRun = null;
let logOffset = null;
let testBusy = false;

function testRequest(action, body) {
  return request(`/api/graphflow/test/${action}`, {
    method: body ? "POST" : "GET",
    headers: { "X-StarWish-Operator-Key": testKey.value, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
}

function testButton(container, text, activate) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.addEventListener("click", () => { if (!testBusy) activate(); });
  container.append(button);
}

function resetTestDisplay() {
  profile = null;
  logRun = null;
  logOffset = null;
  testItems.replaceChildren();
  testRecords.replaceChildren();
  testPlan.textContent = "";
  testLog.textContent = "";
  testLinkStatus.textContent = "";
  testPlan.hidden = testRun.hidden = testLog.hidden = testNext.hidden = true;
}

async function testAction(fn) {
  if (testBusy) return;
  testBusy = testKey.disabled = testLoad.disabled = testRun.disabled = testNext.disabled = testTask.disabled = true;
  try {
    await fn();
  } catch (failure) {
    testStatus.textContent = failure.message;
  } finally {
    testBusy = testKey.disabled = testLoad.disabled = testRun.disabled = testNext.disabled = testTask.disabled = false;
  }
}

function renderTestRecords(records) {
  testRecords.replaceChildren();
  for (const item of records) {
    const status = testStates[item.state] || item.state;
    const code = item.exit_code === null ? "" : ` · 退出码 ${item.exit_code}`;
    testButton(testRecords, `${status} · ${item.profile}${code} · ${item.run_id.slice(0, 8)}`, () =>
      testAction(async () => {
        logRun = item.run_id;
        testLog.hidden = testNext.hidden = true;
        await showTestLog(0);
      }));
  }
}

async function showTestLog(offset) {
  const params = new URLSearchParams({ run_id: logRun, offset: String(offset) });
  const page = await testRequest(`log?${params}`);
  testLog.textContent = page.text || "该页没有日志。";
  testLog.hidden = false;
  logOffset = page.next_offset;
  testNext.hidden = logOffset === null;
  testStatus.textContent = `${testStates[page.state] || page.state}${page.truncated ? " · 日志已达到保存上限" : ""}`;
}

testKey.addEventListener("input", () => {
  resetTestDisplay();
  testStatus.textContent = "";
});

byId("gf-test-form").addEventListener("submit", (event) => {
  event.preventDefault();
  testAction(async () => {
    resetTestDisplay();
    const [plan, records] = await Promise.all([testRequest("plan"), testRequest("records")]);
    for (const item of plan.profiles) {
      const reason = item.via_impact ? "静态依赖" : "直接匹配";
      testButton(testItems, `${item.profile} · ${reason} ${item.matched_count} 个路径`, () => {
        profile = item;
        testPlan.textContent = [`程序与参数：${JSON.stringify(item.argv)}`, `超时：${item.timeout} 秒`,
          ...item.matched, `审批哈希：${item.approval_sha256}`].join("\n");
        testPlan.hidden = testRun.hidden = false;
      });
    }
    renderTestRecords(records);
    const note = plan.impact_status === "partial" ? "静态定位结果不完整。" :
      plan.impact_status?.startsWith("unavailable_") ? "静态定位暂不可用，仅显示直接匹配。" : "";
    testStatus.textContent = `${plan.changed_count} 个改动路径 · ${plan.profiles.length} 个可运行测试。${note}`;
  });
});

testRun.addEventListener("click", () => {
  if (!profile || testBusy) return;
  const taskId = byId("gf-test-task-row").hidden ? "" : testTask.value.trim();
  if (taskId && !/^[0-9a-f]{32}$/.test(taskId)) {
    testLinkStatus.textContent = "请填写完整的 32 位任务编号。";
    testTask.focus();
    return;
  }
  const link = taskId ? `\n测试结束后回填任务 ${taskId} 的证据，不改变任务状态。` : "";
  if (!window.confirm(`运行预设测试「${profile.profile}」？\n${JSON.stringify(profile.argv)}${link}`)) return;
  testAction(async () => {
    testStatus.textContent = "正在运行测试…";
    testLinkStatus.textContent = "";
    const outcome = await testRequest("run", { profile: profile.profile, approval_sha256: profile.approval_sha256,
      ...(taskId ? { task_id: taskId } : {}) });
    if (outcome.task_verification) {
      testLinkStatus.textContent = outcome.task_verification.status === "linked"
        ? `测试证据已写入任务 ${taskId}。`
        : `测试已结束，但未关联任务。${outcome.task_verification.reason} 可手动引用 test:${outcome.run_id}。`;
    }
    renderTestRecords(await testRequest("records"));
    logRun = outcome.run_id;
    await showTestLog(0);
  });
});

testNext.addEventListener("click", () => {
  if (logRun && logOffset !== null) testAction(() => showTestLog(logOffset));
});

const workflows = byId("gf-workflow");
const workflowSelect = byId("gf-workflow-select");
const workflowTarget = byId("gf-workflow-target");
const workflowQuery = byId("gf-workflow-query");
const workflowPreview = byId("gf-workflow-preview");
const workflowReplay = byId("gf-workflow-replay");
const workflowStatus = byId("gf-workflow-status");
const workflowOutput = byId("gf-workflow-output");
const workflowPin = byId("gf-workflow-pin");
const workflowUnpin = byId("gf-workflow-unpin");
const workflowPinKey = byId("gf-workflow-pin-key");
const operations = { read: "读取代码", read_batch: "批量读取代码", impact: "分析影响", changes: "查看 Git 改动",
  cochange: "分析 Git 历史共改（仅建议）", test_plan: "生成测试建议（不运行）" };
let workflowCatalog = null;
let workflowBusy = false;
let workflowReceipt = null;
let workflowSaveRequest = null;

function workflowControls() {
  const persistence = workflowCatalog?.find((item) => item.name === workflowSelect.value)?.persistence;
  byId("gf-workflow-pin-controls").hidden = !persistence;
  workflowPin.hidden = persistence !== "candidate";
  workflowPin.disabled = workflowBusy || !workflowReceipt || !workflowPinKey.value;
  workflowUnpin.hidden = persistence !== "pinned";
  workflowUnpin.disabled = workflowBusy || !workflowPinKey.value;
  workflowSelect.disabled = workflowBusy || !workflowCatalog?.length;
  workflowPreview.disabled = workflowSelect.disabled;
  workflowReplay.disabled = workflowBusy || !workflowReceipt;
  byId("gf-task-save").disabled = workflowBusy || !workflowReceipt;
  workflowTarget.disabled = workflowQuery.disabled = workflowBusy;
}

function clearWorkflowReceipt() {
  workflowReceipt = null;
  workflowSaveRequest = null;
  workflowOutput.textContent = "";
  workflowOutput.hidden = true;
  workflowStatus.textContent = "";
  workflowControls();
}

function workflowInputState() {
  const params = workflowCatalog?.find((item) => item.name === workflowSelect.value)?.parameters || [];
  for (const [key, input] of [["target", workflowTarget], ["query", workflowQuery]]) {
    const required = params.includes(key);
    byId(`gf-workflow-${key}-row`).hidden = !required;
    input.required = required;
  }
  clearWorkflowReceipt();
}

async function workflowAction(fn) {
  if (workflowBusy) return;
  workflowBusy = true;
  workflowControls();
  try {
    await fn();
  } catch (failure) {
    workflowStatus.textContent = failure.message;
  } finally {
    workflowBusy = false;
    workflowControls();
  }
}

function workflowRequest(action, body, operator = false) {
  const headers = { "Content-Type": "application/json" };
  if (operator) headers["X-StarWish-Operator-Key"] = workflowPinKey.value;
  return request(`/api/graphflow/workflow/${action}`, {
    method: "POST", headers, body: JSON.stringify(body),
  });
}

async function refreshWorkflows() {
  const current = workflowSelect.value;
  const items = await request("/api/graphflow/workflow/catalog");
  workflowCatalog = items;
  workflowSelect.replaceChildren();
  for (const item of items) {
    workflowSelect.add(new Option(item.title + (item.persistence === "pinned" ? " · 已固定" : ""), item.name));
  }
  if (items.some((item) => item.name === current)) workflowSelect.value = current;
  workflowInputState();
}

workflows.addEventListener("toggle", () => {
  if (!workflows.open) return;
  workflowAction(refreshWorkflows);
});
workflowSelect.addEventListener("change", workflowInputState);
workflowTarget.addEventListener("input", clearWorkflowReceipt);
workflowQuery.addEventListener("input", clearWorkflowReceipt);
workflowPinKey.addEventListener("input", workflowControls);

workflowPin.addEventListener("click", () => {
  if (!workflowReceipt) return;
  workflowAction(async () => {
    const { name, plan_sha256 } = workflowReceipt;
    await workflowRequest("pin", { name, plan_sha256 }, true);
    await refreshWorkflows();
    workflowStatus.textContent = "已固定。即使关闭轨迹采集，也能预览和重放。";
  });
});

workflowUnpin.addEventListener("click", () => {
  workflowAction(async () => {
    await workflowRequest("unpin", { name: workflowSelect.value }, true);
    await refreshWorkflows();
    workflowStatus.textContent = "已取消固定。近期轨迹仍满足条件时，候选仍可使用。";
  });
});

byId("gf-workflow-form").addEventListener("submit", (event) => {
  event.preventDefault();
  workflowAction(async () => {
    clearWorkflowReceipt();
    const inputs = { name: workflowSelect.value, target: workflowTarget.required ? workflowTarget.value : "",
      query: workflowQuery.required ? workflowQuery.value : "" };
    const plan = await workflowRequest("preview", inputs);
    workflowReceipt = { ...inputs, plan_sha256: plan.plan_sha256 };
    workflowOutput.textContent = plan.steps.map((step, index) => {
      const input = step.targets?.join("、") || step.target || step.query;
      return `${index + 1}. ${operations[step.op]}${input ? " · " + input : ""}`;
    }).join("\n");
    workflowOutput.hidden = false;
    workflowStatus.textContent = "预览已生成，尚未执行。";
  });
});

workflowReplay.addEventListener("click", () => {
  if (!workflowReceipt) return;
  workflowAction(async () => {
    const inputs = workflowReceipt;
    workflowReceipt = null;
    workflowStatus.textContent = "正在运行只读步骤…";
    const report = await workflowRequest("replay", inputs);
    workflowOutput.textContent = report.steps.map((step) =>
      `${operations[step.op]}\n${step.text || step.error || ""}${step.truncated ? "\n（已截断）" : ""}`).join("\n\n");
    workflowStatus.textContent = report.state === "completed" ? "工作流已完成；未运行测试。" : "工作流在失败步骤停止。";
  });
});

const taskItems = byId("gf-task-items");
const taskIdInput = byId("gf-task-id");
const taskStatus = byId("gf-task-status");
const taskReport = byId("gf-task-report");
const taskState = byId("gf-task-state");
const taskDetail = byId("gf-task-detail");
const taskLabels = { pending: "待处理", blocked: "已阻塞", completed: "已完成" };
let selectedTask = null;
let taskBusy = false;

function taskRequest(action, body) {
  return request(`/api/graphflow/tasks/${action}`, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
}

async function taskAction(fn) {
  if (taskBusy) return;
  taskBusy = true;
  try {
    await fn();
  } catch (failure) {
    taskStatus.textContent = failure.message;
  } finally {
    taskBusy = false;
  }
}

async function loadTask(id) {
  selectedTask = null;
  taskReport.hidden = taskDetail.hidden = true;
  const task = await taskRequest(`detail?${new URLSearchParams({ task_id: id })}`);
  selectedTask = task;
  taskIdInput.value = task.id;
  taskDetail.textContent = JSON.stringify({ plan: task.plan, events: task.events, test_verifications: task.test_verifications }, null, 2);
  taskDetail.hidden = false;
  taskState.replaceChildren();
  if (task.state !== "completed") {
    for (const next of [task.state === "pending" ? "blocked" : "pending", "completed"]) {
      taskState.add(new Option(taskLabels[next], next));
    }
  }
  taskReport.hidden = task.state === "completed";
  taskStatus.textContent = `${taskLabels[task.state]} · 版本 ${task.revision}`;
}

byId("gf-task-save").addEventListener("click", () => {
  if (!workflowReceipt) return;
  workflowAction(async () => {
    workflowSaveRequest ||= crypto.randomUUID();
    const saved = await taskRequest("create", { ...workflowReceipt, request_id: workflowSaveRequest });
    workflowStatus.textContent = `已存入任务台账：${saved.id}`;
    byId("gf-tasks").open = true;
    await taskAction(() => loadTask(saved.id));
  });
});

byId("gf-task-load").addEventListener("click", () => taskAction(async () => {
  const report = await taskRequest("backlog");
  taskItems.replaceChildren();
  for (const item of report.items) {
    testButton(taskItems, `${taskLabels[item.state]} · ${item.workflow} · ${item.id.slice(0, 8)}`, () =>
      taskAction(() => loadTask(item.id)));
  }
  taskStatus.textContent = `待处理 ${report.counts.pending} · 阻塞 ${report.counts.blocked} · 超过 24 小时 ${report.older_than_24h}${report.more ? "（只显示最早 20 条）" : ""}`;
}));

byId("gf-task-detail-form").addEventListener("submit", (event) => {
  event.preventDefault();
  taskAction(() => loadTask(taskIdInput.value));
});

taskIdInput.addEventListener("input", () => {
  selectedTask = null;
  taskReport.hidden = taskDetail.hidden = true;
});

taskReport.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!selectedTask) return;
  taskAction(async () => {
    const id = selectedTask.id;
    await taskRequest("report", { task_id: id, revision: selectedTask.revision, state: taskState.value,
      summary: byId("gf-task-summary").value, references: byId("gf-task-refs").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean) });
    byId("gf-task-summary").value = byId("gf-task-refs").value = "";
    await loadTask(id);
  });
});

const legacyForm = byId("gf-legacy-form");
const legacyId = byId("gf-legacy-id");
const legacyTarget = byId("gf-legacy-target");
const legacyLoad = byId("gf-legacy-load");
const legacyNext = byId("gf-legacy-next");
const legacyPreview = byId("gf-legacy-preview");
const legacyApply = byId("gf-legacy-apply");
const legacyApplyRow = byId("gf-legacy-apply-row");
const legacyPlan = byId("gf-legacy-plan");
const legacyStatus = byId("gf-legacy-status");
let legacyCursor = null;
let legacyReceipt = null;
let legacyBusy = false;

function resetLegacyPreview() {
  legacyReceipt = null;
  legacyPlan.textContent = "";
  legacyPlan.hidden = legacyApplyRow.hidden = true;
}

async function legacyAction(fn) {
  if (legacyBusy) return;
  const controls = [legacyId, legacyTarget, legacyLoad, legacyNext, legacyPreview, legacyApply];
  legacyBusy = true;
  controls.forEach((el) => { el.disabled = true; });
  try {
    await fn();
  } catch (failure) {
    legacyStatus.textContent = failure.message;
  } finally {
    controls.forEach((el) => { el.disabled = false; });
    legacyBusy = false;
  }
}

async function loadLegacyPage(after = "") {
  resetLegacyPreview();
  legacyCursor = null;
  legacyForm.hidden = legacyNext.hidden = true;
  legacyId.replaceChildren();
  legacyStatus.textContent = "正在读取旧待办编号…";
  const result = await taskRequest(`legacy?${new URLSearchParams({ after })}`);
  for (const id of result.legacy_ids) legacyId.add(new Option(id, id));
  legacyCursor = result.next_after;
  legacyNext.hidden = !legacyCursor;
  legacyForm.hidden = !result.legacy_ids.length;
  legacyStatus.textContent = result.legacy_ids.length ? `本页 ${result.legacy_ids.length} 个旧编号。` : "没有可列出的旧待办。";
}

legacyLoad.addEventListener("click", () => legacyAction(() => loadLegacyPage()));
legacyNext.addEventListener("click", () => {
  if (legacyCursor) legacyAction(() => loadLegacyPage(legacyCursor));
});
for (const input of [legacyId, legacyTarget]) {
  input.addEventListener("input", () => {
    resetLegacyPreview();
    legacyStatus.textContent = "";
  });
}
legacyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  legacyAction(async () => {
    resetLegacyPreview();
    const input = { legacy_id: legacyId.value, target: legacyTarget.value.trim() };
    legacyStatus.textContent = "正在核对当前源码符号…";
    const preview = await taskRequest("legacy/preview", input);
    const anchor = preview.anchor;
    legacyPlan.textContent = [`旧编号：${preview.legacy_id}`,
      `当前定位：${anchor.file}#${anchor.symbol}（第 ${anchor.start_line}–${anchor.end_line} 行）`,
      `待办计划：${preview.plan.steps.map((step) => operations[step.op] || step.op).join(" → ")}`].join("\n");
    legacyReceipt = { ...input, confirm_sha256: preview.confirm_sha256 };
    legacyPlan.hidden = legacyApplyRow.hidden = false;
    legacyStatus.textContent = "预览未写入台账。核对旧编号和当前源码后再确认。";
  });
});
legacyApply.addEventListener("click", () => {
  if (!legacyReceipt) return;
  legacyAction(async () => {
    const receipt = legacyReceipt;
    resetLegacyPreview();
    const result = await taskRequest("legacy/import", receipt);
    legacyStatus.textContent = `${result.ledger_written ? "已接手为待办" : "此前已接手"}：${result.task.id}`;
    await taskAction(() => loadTask(result.task.id));
  });
});

const planner = byId("gf-planner-form");
const plannerSubmit = byId("gf-planner-submit");
const plannerOutput = byId("gf-planner-output");
const plannerStatus = byId("gf-planner-status");

planner.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (plannerSubmit.disabled) return;
  plannerSubmit.disabled = true;
  plannerOutput.hidden = true;
  plannerStatus.textContent = "正在核对代码位置…";
  try {
    const report = await request("/api/graphflow/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: byId("gf-planner-task").value, target: byId("gf-planner-target").value,
        intent: byId("gf-planner-intent").value, mode: byId("gf-planner-insight").checked ? "insight" : "simple" }),
    });
    const text = report.plan.steps.map((step, index) => {
      const gate = step.gate.includes("operator_approval") ? "（需本机审批）" : "";
      return `${index + 1}. ${step.title}${gate}\n   ${step.description}`;
    });
    if (report.context.anchors.length) {
      text.push("代码位置：", ...report.context.anchors.map((anchor) => `  ${anchor.read_target} · ${anchor.symbol}`));
    }
    text.push("待核对：", ...report.challenges.map((item) => `  ${item.question}`));
    if (report.insight) text.push("分析问题：", ...Object.values(report.insight.six_hats_questions),
      ...report.insight.five_whys);
    plannerOutput.textContent = text.join("\n");
    plannerOutput.hidden = false;
    plannerStatus.textContent = "计划已生成，未执行或保存。";
  } catch (failure) {
    plannerStatus.textContent = failure.message;
  } finally {
    plannerSubmit.disabled = false;
  }
});

request("/api/graphflow/status").then(setAvailability).catch((failure) => {
  setAvailability({ reason: failure.message });
});
