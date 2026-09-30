"use strict";

/** Adapt AG-UI to the shared conversation renderer without retaining a second transcript. */
const harnestAgui = (() => {
  const pending = new Set();
  const node = (tag, text = "") => { const element = document.createElement(tag); element.textContent = text; return element; };

  /** Send only this turn; Harnest owns history and private results never enter messages or state. */
  async function run(threadId, input, resume) {
    const response = await api("/agui", {method:"POST", body:JSON.stringify({
      threadId, runId:crypto.randomUUID(), messages:input ? [{id:crypto.randomUUID(), role:"user", content:input}] : [],
      state:{}, tools:[], context:[], forwardedProps:{}, ...(resume ? {resume} : {}),
    })});
    const calls = new Map(), actions = new Map();
    let finished = false, waiting = false;
    await consumeSse(response, event => {
      if (event.type === "RUN_ERROR") throw new Error(event.message || "AG-UI run failed");
      if (event.type === "RUN_STARTED") beginStreamingOutput();
      if (event.type === "TEXT_MESSAGE_START") runtime.streamingBubble = null;
      if (event.type === "TEXT_MESSAGE_CONTENT") appendStreamingText(event.delta || "");
      if (event.type === "TEXT_MESSAGE_END") runtime.streamingBubble = null;
      if (event.type === "TOOL_CALL_START") calls.set(event.toolCallId, {name:event.toolCallName, arguments:""});
      if (event.type === "TOOL_CALL_ARGS" && calls.has(event.toolCallId)) calls.get(event.toolCallId).arguments += event.delta;
      if (event.type === "TOOL_CALL_END") {
        const call = calls.get(event.toolCallId);
        if (call) { beginToolBoundary(); appendToolCall(call.name, decode(call.arguments), event.toolCallId); }
      }
      if (event.type === "TOOL_CALL_RESULT") appendToolResult(calls.get(event.toolCallId)?.name || "Client tool", decode(event.content), event.toolCallId);
      if (event.type === "CUSTOM") custom(event, actions);
      // State is refreshed from the authoritative session after the stream. Do not
      // merge arbitrary JSON pointers into browser objects or replay snapshots.
      if (event.type === "RUN_FINISHED") {
        finished = true;
        for (const interrupt of event.outcome?.interrupts || []) {
          const action = interrupt.metadata?.harnest;
          if (action?.id) actions.set(action.id, action);
        }
        waiting = event.result?.status === "in_progress";
        if (event.result !== undefined && !waiting) appendResult(event.result);
      }
    });
    if (!finished) throw new Error("AG-UI stream ended before the run finished");
    pending.delete(threadId);
    clearTypingIndicator();
    if (actions.size) { pending.add(threadId); renderActions(threadId, [...actions.values()]); }
    await finishRequest(actions.size ? "Agent action required" : waiting ? "Waiting for external work" : "AG-UI response complete");
    if (actions.size || waiting) setStatus(actions.size ? "Agent action required" : "Waiting for external work", "pending");
  }

  function decode(value) { try { return JSON.parse(value); } catch { return value; } }

  /** Preserve Harnest-specific events while collecting one complete continuation batch. */
  function custom(event, actions) {
    if (!["harnest.client_tool", "harnest.external_wait", "thinking", "agent_activity", "agent_metadata", "decision_result"].includes(event.name)) appendUIEvent(event);
    if (event.name === "harnest.client_tool") actions.set(event.value.id, event.value);
    if (event.name === "thinking") appendThinking(event.value.text || "", event.value.agent);
    if (event.name === "agent_activity") appendAgentActivity(event.value);
    if (event.name === "agent_metadata") appendAgentMetadata(event.value);
    if (event.name === "decision_result") appendDecisionResult(event.value);
  }

  /** Present every pending action together: the runtime accepts an atomic continuation batch. */
  function renderActions(threadId, actions) {
    const form = node("form"); form.className = "approval-event agui-actions";
    form.append(node("strong", "Continue the agent"), node("p", "Complete every pending action, then continue. Private input is sent only to its declared handler."));
    const fields = actions.map(action => actionField(form, action));
    const error = node("p"); error.setAttribute("role", "alert");
    const submit = node("button", "Continue"); submit.className = "secondary-button"; submit.type = "submit";
    const cancel = node("button", "Cancel pending actions"); cancel.className = "secondary-button"; cancel.type = "button";
    form.append(error, submit, cancel);
    const send = async cancelled => {
      if (runtime.busy) return;
      if (runtime.sessionId !== threadId) { error.textContent = "Return to the original session to continue."; return; }
      let replies;
      try { replies = actions.map((action, index) => reply(action, fields[index], cancelled)); }
      catch { error.textContent = "Choose each approval decision and enter valid JSON for each input."; return; }
      finally { fields.forEach((field, index) => { if (actions[index].privateInput) field.value = ""; }); }
      submit.disabled = cancel.disabled = true; error.textContent = "";
      startAgentResume("Continuing AG-UI run…");
      try {
        await run(threadId, null, replies);
        form.replaceChildren(node("small", cancelled ? "Pending actions cancelled." : "Actions submitted."));
      } catch (failure) {
        submit.disabled = cancel.disabled = false;
        if ([404, 409, 410].includes(failure.status)) { pending.delete(threadId); form.replaceChildren(node("p", "These actions are no longer available. Send a new message to continue.")); }
        finishFailedRequest(failure);
      }
    };
    form.addEventListener("submit", event => { event.preventDefault(); return send(false); });
    cancel.addEventListener("click", () => send(true));
    ui.conversation.append(form); scrollConversation();
  }

  /** Keep browser values out of transcript, storage, and validation errors. */
  function reply(action, field, cancelled) {
    if (cancelled) return {interruptId:action.id, status:"cancelled"};
    if (action.type === "human_approval" && !field.value) throw new Error("Decision required");
    return {interruptId:action.id, status:"resolved", payload:action.type === "human_approval" ? {approved:field.value === "approve"} : JSON.parse(field.value)};
  }

  function actionField(form, action) {
    const label = node("label", action.message || action.name || "Approval");
    const approval = action.type === "human_approval";
    const field = node(approval ? "select" : "textarea"); field.required = true;
    if (approval) {
      for (const [value, text] of [["", "Choose a decision"], ["approve", "Approve"], ["deny", "Deny"]]) {
        const option = node("option", text); option.value = value; field.append(option);
      }
    } else {
      field.rows = 4; field.autocomplete = "off"; field.spellcheck = false;
      label.append(node("pre", pretty(action.privateInput ? action.inputSchema : action.outputSchema)));
    }
    label.append(field); form.append(label); return field;
  }

  return {run, pending:threadId => pending.has(threadId)};
})();
