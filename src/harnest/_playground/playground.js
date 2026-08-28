"use strict";

const ui = {
  agentName: document.querySelector("#agent-name"),
  agentDescription: document.querySelector("#agent-description"),
  agentFramework: document.querySelector("#agent-framework"),
  agentMode: document.querySelector("#agent-mode"),
  sessionSelect: document.querySelector("#session-select"),
  sessionState: document.querySelector("#session-state"),
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
};

const runtime = {
  sessionId: "",
  transport: "stream",
  socket: null,
  liveSessionId: "",
  busy: false,
  streamingBubble: null,
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

function appendTool(title, value) {
  const detail = document.createElement("details");
  detail.className = "tool-event";
  const summary = document.createElement("summary");
  summary.textContent = title;
  const contents = document.createElement("pre");
  contents.textContent = pretty(value);
  detail.append(summary, contents);
  ui.conversation.append(detail);
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
  if (item.type === "tool_call") appendTool(`Tool call · ${item.name || "unnamed"}`, item.arguments);
  if (item.type === "tool_result") appendTool(`Tool result · ${item.name || item.callId || "unnamed"}`, item.output);
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
  if (!sessions.length) addSessionOption("", "No session");
  for (const session of sessions) addSessionOption(session.id, session.id);
  const available = sessions.some((session) => session.id === preferredId);
  runtime.sessionId = available ? preferredId : (sessions[0]?.id || "");
  ui.sessionSelect.value = runtime.sessionId;
  ui.sessionId.textContent = runtime.sessionId || "Not created";
}

function addSessionOption(value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  ui.sessionSelect.append(option);
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
    ui.sessionState.textContent = "{}";
    ui.sessionId.textContent = "Not created";
    return;
  }
  const response = await api(`${endpoints.sessions}/${encodeURIComponent(runtime.sessionId)}`, { method: "GET" });
  const session = await response.json();
  ui.sessionState.textContent = pretty(session.state || {});
  ui.sessionId.textContent = session.id;
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
  if (frame.type === "response.tool_call") appendTool(`Tool call · ${frame.name || "unnamed"}`, frame.arguments);
  if (frame.type === "response.tool_result") appendTool(`Tool result · ${frame.name || frame.callId || "unnamed"}`, frame.output);
  if (frame.type === "response.completed") finishStreamingOutput(frame);
  if (frame.type === "error") throw new Error(frame.error || "Agent stream failed");
}

function beginStreamingOutput() {
  runtime.streamingBubble = null;
  setStatus("Agent is responding…");
}

function appendStreamingText(delta) {
  if (!runtime.streamingBubble) runtime.streamingBubble = appendTurn("assistant");
  runtime.streamingBubble.textContent += delta;
  scrollConversation();
}

function finishStreamingOutput(frame) {
  if (frame.status === "requires_action") {
    appendApproval(frame.requiredAction);
    runtime.streamingBubble = null;
    return;
  }
  if (!runtime.streamingBubble && frame.outputText) appendTurn("assistant", frame.outputText);
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
    if (runtime.transport !== "live") finishRequest("Response complete");
  } catch (error) {
    finishFailedRequest(error);
  }
}

function startRequest(input) {
  clearError();
  runtime.busy = true;
  ui.send.disabled = true;
  ui.input.value = "";
  appendTurn("user", input);
  setStatus("Sending…");
}

async function finishRequest(message) {
  runtime.busy = false;
  ui.send.disabled = false;
  setStatus(message, "ok");
  try {
    await loadSessionState();
  } catch (error) {
    showError(error);
  }
}

function finishFailedRequest(error) {
  runtime.busy = false;
  ui.send.disabled = false;
  showError(error);
}

function selectTransport(button) {
  runtime.transport = button.dataset.transport;
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
  ui.input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    ui.composer.requestSubmit();
  });
  document.querySelector("#new-session").addEventListener("click", () => runAction(createSession));
  document.querySelector("#refresh-sessions").addEventListener("click", () => runAction(loadSessions));
  document.querySelector("#refresh-state").addEventListener("click", () => runAction(loadSessionState));
  ui.sessionSelect.addEventListener("change", () => changeSession(ui.sessionSelect.value));
  for (const button of document.querySelectorAll(".transport")) {
    button.addEventListener("click", () => selectTransport(button));
  }
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
  closeLiveSocket();
  await runAction(loadSessionState);
  setStatus(sessionId ? "Session selected" : "Create a session to begin", sessionId ? "ok" : "pending");
}

async function initialize() {
  bindEvents();
  try {
    await Promise.all([loadAgent(), loadSessions()]);
    setStatus(runtime.sessionId ? "Ready" : "Create a session to begin", "ok");
  } catch (error) {
    showError(error);
  }
}

initialize();
