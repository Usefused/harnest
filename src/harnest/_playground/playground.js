"use strict";

const ui = {
  agentName: document.querySelector("#agent-name"),
  agentDescription: document.querySelector("#agent-description"),
  agentFramework: document.querySelector("#agent-framework"),
  agentMode: document.querySelector("#agent-mode"),
  sessionSelect: document.querySelector("#session-select"),
  sessionPicker: document.querySelector("#session-picker"),
  sessionTrigger: document.querySelector("#session-trigger"),
  sessionValue: document.querySelector("#session-value"),
  sessionValueDetail: document.querySelector("#session-value-detail"),
  sessionMenu: document.querySelector("#session-menu"),
  sessionState: document.querySelector("#session-state"),
  sessionStateEmpty: document.querySelector("#session-state-empty"),
  sessionId: document.querySelector("#session-id"),
  conversation: document.querySelector("#conversation"),
  emptyState: document.querySelector("#empty-state"),
  composer: document.querySelector("#composer"),
  input: document.querySelector("#message-input"),
  send: document.querySelector("#send-message"),
  status: document.querySelector("#connection-status"),
  statusText: document.querySelector("#status-text"),
  error: document.querySelector("#error-banner"),
  token: document.querySelector("#bearer-token"),
  transportNote: document.querySelector("#transport-note"),
  inspector: document.querySelector("#session-inspector"),
  inspectorToggle: document.querySelector("#toggle-inspector"),
  inspectorRefresh: document.querySelector("#refresh-state"),
  stateTab: document.querySelector("#state-tab"),
  traceTab: document.querySelector("#trace-tab"),
  logsTab: document.querySelector("#logs-tab"),
  stateView: document.querySelector("#state-view"),
  traceView: document.querySelector("#trace-view"),
  logsView: document.querySelector("#logs-view"),
  traceCount: document.querySelector("#trace-count"),
  traceTitle: document.querySelector("#trace-title"),
  traceStatus: document.querySelector("#trace-status"),
  traceRuns: document.querySelector("#trace-runs"),
  traceEmpty: document.querySelector("#trace-empty"),
  traceTimeline: document.querySelector("#trace-timeline"),
  traceId: document.querySelector("#trace-id"),
  logCount: document.querySelector("#log-count"),
  logSummary: document.querySelector("#log-summary"),
  logSearch: document.querySelector("#log-search"),
  logLevels: document.querySelector("#log-levels"),
  logsEmpty: document.querySelector("#logs-empty"),
  logsList: document.querySelector("#logs-list"),
};

const runtime = {
  sessionId: "",
  transport: "stream",
  socket: null,
  liveSessionId: "",
  busy: false,
  streamingBubble: null,
  typingBubble: null,
  inspectorView: "state",
  traces: [],
  selectedTraceId: "",
  logQuery: "",
  logLevel: "all",
  traceTimer: null,
  toolCards: new Map(),
};

const transportNotes = {
  stream: "SSE streams response events as they arrive.",
  response: "Wait for one complete JSON response.",
  live: "WebSocket live mode uses same-origin cookie authentication.",
};

const endpoints = {
  agent: "/agent",
  sessions: "/sessions",
  responses: "/responses",
  live: "/live",
  traces: "/_harnest/traces",
};

function requestHeaders() {
  const headers = { "Content-Type": "application/json" };
  const token = ui.token.value.trim();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: requestHeaders() });
  if (!response.ok) throw await responseError(response);
  return response;
}

async function responseError(response) {
  let detail = `Request failed with HTTP ${response.status}`;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") detail = body.detail;
  } catch (_error) {
    // Status text remains useful when an upstream proxy does not return JSON.
  }
  return new Error(detail);
}

function setStatus(message, tone = "pending") {
  ui.statusText.textContent = message;
  ui.status.dataset.tone = tone;
}

function showError(error) {
  const message = error instanceof Error ? error.message : String(error);
  ui.error.textContent = message;
  ui.error.hidden = false;
  setStatus("Request failed", "error");
}

function clearError() {
  ui.error.hidden = true;
  ui.error.textContent = "";
}

function pretty(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? "null";
}

function appendTurn(role, text = "") {
  ui.emptyState?.remove();
  const turn = document.createElement("article");
  turn.className = `turn ${role}`;
  const label = document.createElement("span");
  label.className = "turn-label";
  label.textContent = role === "user" ? "You" : "Agent";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  turn.append(label, bubble);
  ui.conversation.append(turn);
  scrollConversation();
  return bubble;
}

function showTypingIndicator() {
  clearTypingIndicator();
  const bubble = appendTurn("assistant");
  bubble.classList.add("typing-bubble");
  bubble.setAttribute("aria-label", "Agent is typing");
  for (let index = 0; index < 3; index += 1) {
    const dot = document.createElement("span");
    dot.className = "typing-dot";
    bubble.append(dot);
  }
  runtime.typingBubble = bubble;
}

function takeTypingBubble() {
  const bubble = runtime.typingBubble;
  if (!bubble) return null;
  bubble.replaceChildren();
  bubble.classList.remove("typing-bubble");
  bubble.removeAttribute("aria-label");
  runtime.typingBubble = null;
  return bubble;
}

function clearTypingIndicator() {
  runtime.typingBubble?.closest(".turn")?.remove();
  runtime.typingBubble = null;
}

function toolKey(name, callId) {
  return callId || name || `tool-${runtime.toolCards.size + 1}`;
}

function createToolCard(name, callId) {
  const detail = document.createElement("details");
  detail.className = "tool-event";
  detail.dataset.status = "running";
  detail.open = true;
  const summary = document.createElement("summary");
  const mark = document.createElement("span");
  mark.className = "tool-mark";
  mark.textContent = "↯";
  const heading = document.createElement("span");
  heading.className = "tool-heading";
  const title = document.createElement("strong");
  title.textContent = name || "Unnamed tool";
  const identifier = document.createElement("small");
  identifier.textContent = callId || "Agent tool call";
  heading.append(title, identifier);
  const status = document.createElement("span");
  status.className = "tool-status";
  status.textContent = "Running";
  summary.append(mark, heading, status);
  const sections = document.createElement("div");
  sections.className = "tool-sections";
  detail.append(summary, sections);
  ui.conversation.append(detail);
  return { detail, sections, status };
}

function appendToolSection(card, label, value) {
  const section = document.createElement("section");
  section.className = "tool-section";
  const heading = document.createElement("span");
  heading.className = "tool-section-label";
  heading.textContent = label;
  const contents = document.createElement("pre");
  contents.textContent = pretty(value);
  section.append(heading, contents);
  card.sections.append(section);
}

function appendToolCall(name, value, callId) {
  const key = toolKey(name, callId);
  const card = createToolCard(name, callId);
  appendToolSection(card, "Arguments", value ?? {});
  runtime.toolCards.set(key, card);
  scrollConversation();
}

function appendToolResult(name, value, callId) {
  const key = toolKey(name, callId);
  const card = runtime.toolCards.get(key) || createToolCard(name, callId);
  appendToolSection(card, "Result", value);
  card.detail.dataset.status = "completed";
  card.status.textContent = "Completed";
  runtime.toolCards.set(key, card);
  scrollConversation();
}

function appendResult(value) {
  const panel = document.createElement("div");
  panel.className = "result-event";
  const contents = document.createElement("pre");
  contents.textContent = pretty(value);
  panel.append(contents);
  ui.conversation.append(panel);
  scrollConversation();
}

function scrollConversation() {
  ui.conversation.scrollTop = ui.conversation.scrollHeight;
}

function renderOutput(items, fallback = "") {
  let displayedText = false;
  for (const item of items || []) displayedText = renderOutputItem(item) || displayedText;
  if (!displayedText && fallback) appendTurn("assistant", fallback);
}

function renderOutputItem(item) {
  if (item.type === "message") {
    const text = (item.content || []).map((part) => part.text || "").join("");
    if (text) appendTurn("assistant", text);
    return Boolean(text);
  }
  if (item.type === "tool_call") appendToolCall(item.name, item.arguments, item.id);
  if (item.type === "tool_result") appendToolResult(item.name, item.output, item.callId);
  if (item.type === "output") appendResult(item.value);
  return false;
}

async function loadAgent() {
  const response = await api(endpoints.agent, { method: "GET" });
  const agent = await response.json();
  ui.agentName.textContent = agent.name || agent.id || "Unnamed agent";
  ui.agentDescription.textContent = agent.description || "No description provided.";
  ui.agentFramework.textContent = agent.framework || "custom";
  ui.agentMode.textContent = agent.mode || "managed";
}

async function loadSessions(preferredId = runtime.sessionId) {
  const response = await api(endpoints.sessions, { method: "GET" });
  const body = await response.json();
  replaceSessionOptions(body.sessions || [], preferredId);
  await loadSessionState();
}

function replaceSessionOptions(sessions, preferredId) {
  ui.sessionSelect.replaceChildren();
  ui.sessionMenu.replaceChildren();
  if (!sessions.length) addSessionOption("", "No session", "Create one to begin", 0);
  sessions.forEach((session, index) => addSessionOption(session.id, `Session ${index + 1}`, compactSessionId(session.id), index));
  const available = sessions.some((session) => session.id === preferredId);
  runtime.sessionId = available ? preferredId : (sessions[0]?.id || "");
  ui.sessionSelect.value = runtime.sessionId;
  syncSessionPicker();
  ui.sessionId.textContent = runtime.sessionId || "Not created";
}

function addSessionOption(value, label, detail, index) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  option.dataset.detail = detail;
  ui.sessionSelect.append(option);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "session-option";
  button.dataset.value = value;
  button.setAttribute("role", "option");
  button.setAttribute("aria-label", value ? `${label}, ${value}` : label);
  button.innerHTML = `<span class="session-option-mark" aria-hidden="true">✓</span><span class="session-option-copy"><strong></strong><small></small></span><span class="session-option-status">Active</span>`;
  button.querySelector("strong").textContent = label;
  button.querySelector("small").textContent = detail;
  button.addEventListener("click", () => chooseSession(value));
  button.addEventListener("keydown", (event) => moveSessionFocus(event, index));
  ui.sessionMenu.append(button);
}

function compactSessionId(sessionId) {
  if (!sessionId) return "Create one to begin";
  if (sessionId.length <= 20) return sessionId;
  return `${sessionId.slice(0, 12)}…${sessionId.slice(-6)}`;
}

function syncSessionPicker() {
  const selected = ui.sessionSelect.selectedOptions[0];
  ui.sessionValue.textContent = selected?.textContent || "No session";
  ui.sessionValueDetail.textContent = selected?.dataset.detail || "Create one to begin";
  for (const option of ui.sessionMenu.querySelectorAll(".session-option")) {
    option.setAttribute("aria-selected", String(option.dataset.value === runtime.sessionId));
  }
}

function toggleSessionMenu(force) {
  const open = force ?? ui.sessionMenu.hidden;
  ui.sessionMenu.hidden = !open;
  ui.sessionTrigger.setAttribute("aria-expanded", String(open));
  if (open) ui.sessionMenu.querySelector('[aria-selected="true"]')?.focus();
}

function chooseSession(sessionId) {
  toggleSessionMenu(false);
  ui.sessionSelect.value = sessionId;
  syncSessionPicker();
  changeSession(sessionId);
  ui.sessionTrigger.focus();
}

function moveSessionFocus(event, index) {
  const options = [...ui.sessionMenu.querySelectorAll(".session-option")];
  let target = index;
  if (event.key === "ArrowDown") target = (index + 1) % options.length;
  else if (event.key === "ArrowUp") target = (index - 1 + options.length) % options.length;
  else if (event.key === "Home") target = 0;
  else if (event.key === "End") target = options.length - 1;
  else if (event.key === "Escape") return closeSessionMenu();
  else return;
  event.preventDefault();
  options[target].focus();
}

function closeSessionMenu() {
  toggleSessionMenu(false);
  ui.sessionTrigger.focus();
}

async function createSession() {
  setStatus("Creating session…");
  const response = await api(endpoints.sessions, { method: "POST", body: "{}" });
  const session = await response.json();
  runtime.sessionId = session.id;
  closeLiveSocket();
  await loadSessions(session.id);
  setStatus("Session ready", "ok");
  return session.id;
}

async function ensureSession() {
  return runtime.sessionId || createSession();
}

async function loadSessionState() {
  if (!runtime.sessionId) {
    renderSessionState({});
    ui.sessionId.textContent = "Not created";
    return;
  }
  const response = await api(`${endpoints.sessions}/${encodeURIComponent(runtime.sessionId)}`, { method: "GET" });
  const session = await response.json();
  renderSessionState(session.state || {});
  ui.sessionId.textContent = session.id;
}

function renderSessionState(state) {
  const empty = !state || typeof state !== "object" || Object.keys(state).length === 0;
  ui.sessionState.hidden = false;
  ui.sessionStateEmpty.hidden = !empty;
  ui.sessionState.textContent = pretty(state || {});
}

async function loadTraces(silent = false) {
  if (!runtime.sessionId) {
    renderTraces([]);
    return;
  }
  try {
    const query = new URLSearchParams({ sessionId: runtime.sessionId });
    const response = await api(`${endpoints.traces}?${query}`, { method: "GET" });
    const body = await response.json();
    renderTraces(body.traces || []);
  } catch (error) {
    if (!silent) throw error;
  }
}

function renderTraces(traces) {
  runtime.traces = traces;
  ui.traceCount.textContent = String(traces.length);
  const selected = traces.find((trace) => trace.id === runtime.selectedTraceId) || traces[0];
  runtime.selectedTraceId = selected?.id || "";
  renderTraceRuns();
  renderTrace(selected);
  renderLogs();
}

function renderTraceRuns() {
  ui.traceRuns.replaceChildren();
  runtime.traces.slice(0, 6).forEach((trace, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "trace-run";
    button.textContent = `Run ${runtime.traces.length - index}`;
    button.classList.toggle("active", trace.id === runtime.selectedTraceId || (!runtime.selectedTraceId && index === 0));
    button.addEventListener("click", () => selectTrace(trace.id));
    ui.traceRuns.append(button);
  });
}

function selectTrace(traceId) {
  runtime.selectedTraceId = traceId;
  renderTraceRuns();
  renderTrace(runtime.traces.find((trace) => trace.id === traceId));
}

function renderTrace(trace) {
  const available = Boolean(trace);
  ui.traceEmpty.hidden = available;
  ui.traceTimeline.hidden = !available;
  ui.traceTimeline.replaceChildren();
  ui.traceTitle.textContent = available ? traceTitle(trace) : "No trace yet";
  ui.traceStatus.textContent = trace?.status || "Idle";
  ui.traceStatus.dataset.status = trace?.status || "idle";
  ui.traceId.textContent = trace?.id || "Not created";
  for (const entry of trace?.entries || []) {
    if (entry.category !== "log") ui.traceTimeline.append(traceEntry(entry));
  }
}

function sessionLogs() {
  return runtime.traces.flatMap((trace, index) =>
    (trace.entries || [])
      .filter((entry) => entry.category === "log")
      .map((entry) => ({ entry, trace, run: runtime.traces.length - index })),
  );
}

function normalizedLogLevel(level) {
  const value = String(level || "INFO").toUpperCase();
  if (["CRITICAL", "FATAL", "ERROR"].includes(value)) return "error";
  if (["WARN", "WARNING"].includes(value)) return "warning";
  if (value === "DEBUG") return "debug";
  return "info";
}

function logMatches(item) {
  const level = normalizedLogLevel(item.entry.level);
  if (runtime.logLevel !== "all" && level !== runtime.logLevel) return false;
  const searchable = `${item.entry.message} ${item.entry.level} ${pretty(item.entry.detail || {})}`.toLowerCase();
  return searchable.includes(runtime.logQuery);
}

function renderLogs() {
  const logs = sessionLogs();
  const visible = logs.filter(logMatches);
  ui.logCount.textContent = String(logs.length);
  ui.logSummary.textContent = `${visible.length} of ${logs.length}`;
  ui.logsEmpty.hidden = visible.length > 0;
  ui.logsList.hidden = visible.length === 0;
  ui.logsList.replaceChildren(...visible.map(logEntry));
}

function logEntry(item) {
  const row = document.createElement("li");
  const level = normalizedLogLevel(item.entry.level);
  row.className = "log-entry";
  row.dataset.level = level;
  const heading = document.createElement("div");
  heading.className = "log-entry-heading";
  const badge = document.createElement("span");
  badge.className = "log-level-badge";
  badge.textContent = item.entry.level || "INFO";
  const run = document.createElement("span");
  run.textContent = `Run ${item.run} · +${Math.max(0, Math.round(Number(item.entry.offsetMs) || 0))}ms`;
  heading.append(badge, run);
  const message = document.createElement("strong");
  message.textContent = item.entry.message;
  row.append(heading, message);
  appendTraceDetail(row, item.entry.detail);
  return row;
}

function traceTitle(trace) {
  const duration = trace.durationMs == null ? "in progress" : `${Math.round(trace.durationMs)} ms`;
  const labels = { response: "JSON response", stream: "SSE stream", live: "Live WebSocket" };
  return `${labels[trace.transport] || "Agent request"} · ${duration}`;
}

function traceEntry(entry) {
  const item = document.createElement("li");
  item.className = "trace-entry";
  item.dataset.category = entry.category;
  const mark = document.createElement("span");
  mark.className = "trace-entry-mark";
  mark.textContent = traceMark(entry.category);
  const copy = document.createElement("div");
  copy.className = "trace-entry-copy";
  const title = document.createElement("strong");
  title.textContent = entry.message;
  const category = document.createElement("small");
  category.textContent = entry.category === "log" ? `${entry.level} log` : entry.category;
  copy.append(title, category);
  appendTraceDetail(copy, entry.detail);
  const time = document.createElement("time");
  time.textContent = `+${Math.max(0, Math.round(Number(entry.offsetMs) || 0))}ms`;
  item.append(mark, copy, time);
  return item;
}

function traceMark(category) {
  if (category === "tool") return "↯";
  if (category === "log") return "≡";
  if (category === "error") return "!";
  return "•";
}

function appendTraceDetail(parent, detail) {
  if (!detail || !Object.keys(detail).length) return;
  const disclosure = document.createElement("details");
  disclosure.className = "trace-detail";
  const summary = document.createElement("summary");
  summary.textContent = "Details";
  const contents = document.createElement("pre");
  contents.textContent = pretty(detail);
  disclosure.append(summary, contents);
  parent.append(disclosure);
}

function selectInspectorView(view) {
  runtime.inspectorView = view;
  const tracing = view === "trace";
  const logging = view === "logs";
  ui.stateView.hidden = tracing || logging;
  ui.traceView.hidden = !tracing;
  ui.logsView.hidden = !logging;
  ui.stateTab.classList.toggle("active", !tracing && !logging);
  ui.traceTab.classList.toggle("active", tracing);
  ui.logsTab.classList.toggle("active", logging);
  ui.stateTab.setAttribute("aria-selected", String(!tracing && !logging));
  ui.traceTab.setAttribute("aria-selected", String(tracing));
  ui.logsTab.setAttribute("aria-selected", String(logging));
  ui.inspectorToggle.textContent = logging ? "Logs" : tracing ? "Trace" : "State";
  if (tracing || logging) runAction(loadTraces);
}

function refreshInspector() {
  return runtime.inspectorView === "state" ? loadSessionState() : loadTraces();
}

function selectLogLevel(button) {
  runtime.logLevel = button.dataset.level;
  for (const option of ui.logLevels.querySelectorAll(".log-level")) {
    const active = option === button;
    option.classList.toggle("active", active);
    option.setAttribute("aria-pressed", String(active));
  }
  renderLogs();
}

function startTracePolling() {
  stopTracePolling();
  loadTraces(true);
  runtime.traceTimer = window.setInterval(() => loadTraces(true), 600);
}

function stopTracePolling() {
  if (runtime.traceTimer !== null) window.clearInterval(runtime.traceTimer);
  runtime.traceTimer = null;
}

async function sendResponse(input) {
  const sessionId = await ensureSession();
  if (runtime.transport === "live") return sendLive(input, sessionId);
  const stream = runtime.transport === "stream";
  const response = await api(endpoints.responses, {
    method: "POST",
    body: JSON.stringify({ input, sessionId, stream }),
  });
  if (stream) await consumeSse(response);
  else renderCompleted(await response.json());
}

function renderCompleted(response) {
  clearTypingIndicator();
  if (response.status === "requires_action") {
    appendApproval(response.requiredAction);
    return;
  }
  renderOutput(response.output, response.outputText);
  if (response.result !== undefined && !(response.output || []).some((item) => item.type === "output")) {
    appendResult(response.result);
  }
}

function appendApproval(action) {
  clearTypingIndicator();
  if (!action?.id) throw new Error("Approval response did not include an id");
  const panel = document.createElement("section");
  panel.className = "approval-event";
  const title = document.createElement("strong");
  title.textContent = action.action || "Human approval required";
  const message = document.createElement("p");
  message.textContent = action.message || "Approve this protected action?";
  const controls = document.createElement("div");
  controls.className = "approval-actions";
  const deny = approvalButton("Deny", "deny", "secondary-button");
  const approve = approvalButton("Approve", "approve", "send-button");
  for (const button of [deny, approve]) {
    button.addEventListener("click", () => decideApproval(action.id, button.dataset.decision, panel));
  }
  controls.append(deny, approve);
  panel.append(title, message, controls);
  ui.conversation.append(panel);
  scrollConversation();
  setStatus("Human approval required", "pending");
}

function approvalButton(label, decision, className) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.dataset.decision = decision;
  button.textContent = label;
  return button;
}

async function decideApproval(approvalId, decision, panel) {
  const buttons = panel.querySelectorAll("button");
  for (const button of buttons) button.disabled = true;
  clearError();
  try {
    const response = await api(`/approvals/${encodeURIComponent(approvalId)}`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
    const body = await response.json();
    panel.dataset.status = body.status;
    if (body.status === "completed") renderCompleted(body);
    setStatus(body.status === "denied" ? "Action denied" : "Approved response complete", "ok");
    await loadSessionState();
  } catch (error) {
    for (const button of buttons) button.disabled = false;
    showError(error);
  }
}

async function consumeSse(response) {
  if (!response.body) throw new Error("Streaming response did not include a body");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += value || "";
    const parsed = consumeSseBuffer(buffer, done);
    buffer = parsed.rest;
    for (const frame of parsed.frames) handleStreamFrame(frame);
    if (done) break;
  }
}

function consumeSseBuffer(buffer, done) {
  const chunks = buffer.replaceAll("\r\n", "\n").split("\n\n");
  const rest = done ? "" : chunks.pop();
  return { frames: chunks.map(parseSseFrame).filter(Boolean), rest };
}

function parseSseFrame(chunk) {
  const data = chunk.split("\n").find((line) => line.startsWith("data: "));
  return data ? JSON.parse(data.slice(6)) : null;
}

function handleStreamFrame(frame) {
  if (frame.type === "response.created") beginStreamingOutput();
  if (frame.type === "response.text.delta") appendStreamingText(frame.delta || "");
  if (frame.type === "response.tool_call") {
    clearTypingIndicator();
    appendToolCall(frame.name, frame.arguments, frame.id);
  }
  if (frame.type === "response.tool_result") {
    appendToolResult(frame.name, frame.output, frame.callId);
    if (runtime.busy && !runtime.streamingBubble) showTypingIndicator();
  }
  if (frame.type === "response.completed") finishStreamingOutput(frame);
  if (frame.type === "error") throw new Error(frame.error || "Agent stream failed");
}

function beginStreamingOutput() {
  runtime.streamingBubble = null;
}

function appendStreamingText(delta) {
  if (!runtime.streamingBubble) runtime.streamingBubble = takeTypingBubble() || appendTurn("assistant");
  runtime.streamingBubble.textContent += delta;
  scrollConversation();
}

function finishStreamingOutput(frame) {
  if (frame.status === "requires_action") {
    appendApproval(frame.requiredAction);
    runtime.streamingBubble = null;
    return;
  }
  if (!runtime.streamingBubble && frame.outputText) {
    const bubble = takeTypingBubble() || appendTurn("assistant");
    bubble.textContent = frame.outputText;
  } else if (!frame.outputText) {
    clearTypingIndicator();
  }
  const graphOutputs = (frame.output || []).filter((item) => item.type === "output");
  for (const output of graphOutputs) appendResult(output.value);
  if (frame.result !== undefined && !graphOutputs.length) appendResult(frame.result);
  runtime.streamingBubble = null;
}

async function sendLive(input, sessionId) {
  const socket = await ensureLiveSocket(sessionId);
  beginStreamingOutput();
  socket.send(JSON.stringify({
    type: "response.create",
    requestId: crypto.randomUUID(),
    input,
  }));
}

function ensureLiveSocket(sessionId) {
  if (runtime.socket?.readyState === WebSocket.OPEN && runtime.liveSessionId === sessionId) {
    return Promise.resolve(runtime.socket);
  }
  closeLiveSocket();
  return openLiveSocket(sessionId);
}

function openLiveSocket(sessionId) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}${endpoints.live}`);
  runtime.socket = socket;
  runtime.liveSessionId = sessionId;
  setStatus("Connecting live session…");
  return new Promise((resolve, reject) => {
    socket.addEventListener("open", () => socket.send(JSON.stringify({ type: "connect", sessionId })));
    socket.addEventListener("message", (event) => handleLiveMessage(event, socket, resolve, reject));
    socket.addEventListener("error", () => reject(new Error("Live connection failed")), { once: true });
    socket.addEventListener("close", () => {
      handleLiveClose();
      reject(new Error("Live connection closed before the session was ready"));
    });
  });
}

function handleLiveMessage(event, socket, resolve, reject) {
  const frame = JSON.parse(event.data);
  if (frame.type === "session.connected") {
    setStatus("Live session connected", "ok");
    resolve(socket);
    return;
  }
  try {
    handleStreamFrame(frame);
    if (frame.type === "response.completed") {
      const message = frame.status === "requires_action" ? "Approval required" : "Live response complete";
      finishRequest(message);
    }
  } catch (error) {
    reject(error);
    finishFailedRequest(error);
  }
}

function handleLiveClose() {
  const interrupted = runtime.transport === "live" && runtime.busy;
  runtime.socket = null;
  runtime.liveSessionId = "";
  if (interrupted) {
    // A transport failure must release the composer; otherwise one dropped
    // socket leaves the playground unable to send another request.
    finishFailedRequest(new Error("Live connection closed before the response completed"));
  } else if (runtime.transport === "live") {
    setStatus("Live disconnected", "error");
  }
}

function closeLiveSocket() {
  if (runtime.socket) runtime.socket.close(1000, "Session changed");
  runtime.socket = null;
  runtime.liveSessionId = "";
}

async function submitMessage(event) {
  event.preventDefault();
  const input = ui.input.value.trim();
  if (!input || runtime.busy) return;
  startRequest(input);
  try {
    await sendResponse(input);
    if (runtime.transport !== "live") await finishRequest("Response complete");
  } catch (error) {
    finishFailedRequest(error);
  }
}

function startRequest(input) {
  clearError();
  runtime.selectedTraceId = "";
  runtime.busy = true;
  ui.send.disabled = true;
  ui.input.value = "";
  resizeComposer();
  appendTurn("user", input);
  showTypingIndicator();
  startTracePolling();
}

async function finishRequest(message) {
  runtime.busy = false;
  ui.send.disabled = false;
  setStatus(message, "ok");
  stopTracePolling();
  try {
    await Promise.all([loadSessionState(), loadTraces()]);
  } catch (error) {
    showError(error);
  }
}

function finishFailedRequest(error) {
  runtime.busy = false;
  ui.send.disabled = false;
  clearTypingIndicator();
  stopTracePolling();
  loadTraces(true);
  showError(error);
}

function selectTransport(button) {
  runtime.transport = button.dataset.transport;
  button.closest(".segmented").dataset.active = runtime.transport;
  for (const candidate of document.querySelectorAll(".transport")) {
    const active = candidate === button;
    candidate.classList.toggle("active", active);
    candidate.setAttribute("aria-checked", String(active));
  }
  ui.transportNote.textContent = transportNotes[runtime.transport];
  if (runtime.transport !== "live") closeLiveSocket();
  setStatus(`${button.textContent.trim()} mode ready`, "ok");
}

function bindEvents() {
  ui.composer.addEventListener("submit", submitMessage);
  ui.input.addEventListener("input", resizeComposer);
  ui.input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    ui.composer.requestSubmit();
  });
  document.querySelector("#new-session").addEventListener("click", () => runAction(createSession));
  document.querySelector("#refresh-sessions").addEventListener("click", () => runAction(loadSessions));
  ui.inspectorRefresh.addEventListener("click", () => runAction(refreshInspector));
  ui.stateTab.addEventListener("click", () => selectInspectorView("state"));
  ui.traceTab.addEventListener("click", () => selectInspectorView("trace"));
  ui.logsTab.addEventListener("click", () => selectInspectorView("logs"));
  ui.logSearch.addEventListener("input", () => {
    runtime.logQuery = ui.logSearch.value.trim().toLowerCase();
    renderLogs();
  });
  for (const button of ui.logLevels.querySelectorAll(".log-level")) {
    button.addEventListener("click", () => selectLogLevel(button));
  }
  ui.inspectorToggle.addEventListener("click", toggleInspector);
  ui.sessionTrigger.addEventListener("click", () => toggleSessionMenu());
  ui.sessionTrigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      toggleSessionMenu(true);
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!ui.sessionPicker.contains(event.target)) toggleSessionMenu(false);
  });
  ui.sessionSelect.addEventListener("change", () => changeSession(ui.sessionSelect.value));
  for (const button of document.querySelectorAll(".transport")) {
    button.addEventListener("click", () => selectTransport(button));
  }
}

function toggleInspector() {
  const open = ui.inspector.classList.toggle("open");
  ui.inspectorToggle.setAttribute("aria-expanded", String(open));
}

function resizeComposer() {
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 160)}px`;
}

async function runAction(action) {
  clearError();
  try {
    await action();
  } catch (error) {
    showError(error);
  }
}

async function changeSession(sessionId) {
  runtime.sessionId = sessionId;
  runtime.selectedTraceId = "";
  ui.sessionSelect.value = sessionId;
  syncSessionPicker();
  closeLiveSocket();
  await runAction(() => Promise.all([loadSessionState(), loadTraces()]));
  setStatus(sessionId ? "Session selected" : "No session", sessionId ? "ok" : "pending");
}

async function initialize() {
  bindEvents();
  resizeComposer();
  try {
    await Promise.all([loadAgent(), loadSessions()]);
    await loadTraces(true);
    setStatus(runtime.sessionId ? "Ready" : "No session", "ok");
  } catch (error) {
    showError(error);
  }
}

initialize();
