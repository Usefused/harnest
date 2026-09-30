"use strict";

/** Administer the serving runtime with its own authentication and owner-scoped API. */
const harnestWork = (() => {
  let api, host, capabilities, scope = "self", generation = 0;
  const node = (tag, text = "") => { const result = document.createElement(tag); result.textContent = text; return result; };
  const path = (resource = "", selectedScope = scope) => `/_harnest/work${resource}?scope=${encodeURIComponent(selectedScope)}`;
  async function request(resource, method = "GET", body, selectedScope = scope) {
    const response = await api(path(resource, selectedScope), {method, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    return response.json();
  }
  function button(label, action) {
    const result = node("button", label); result.type = "button"; result.className = "secondary-button";
    result.addEventListener("click", async () => {
      result.disabled = true;
      try { await action(); } catch (error) { showError(error); } finally { result.disabled = false; }
    });
    return result;
  }

  /** Refresh current capabilities on every visit so restart or login changes are visible. */
  async function open(client) {
    api = client; host = document.querySelector("#work-workspace");
    const ticket = ++generation, result = await request();
    if (ticket !== generation) return;
    capabilities = result; host.replaceChildren();
    if (!capabilities.available) { host.append(node("p", capabilities.message)); return; }
    if (!capabilities.automation) scope = "self";
    const toolbar = node("div"); toolbar.className = "work-toolbar";
    const label = node("label", "Ownership "), picker = node("select");
    for (const [value, title] of [["self", "My work"], ...(capabilities.automation ? [["automation", "Application automation"]] : [])]) {
      const option = node("option", title); option.value = value; picker.append(option);
    }
    picker.value = scope; picker.addEventListener("change", () => { scope = picker.value; runAction(() => open(api)); });
    label.append(picker); toolbar.append(label, button("Refresh", () => open(api)));
    if (capabilities.cron && capabilities.targets.length) toolbar.append(button("New schedule", () => editSchedule()));
    host.append(toolbar, node("p", "Live work for the selected owner. Cancelling a running task revokes its lease; external side effects already performed cannot be undone."));
    const tasks = section("Tasks"), crons = section("Dynamic schedules"); host.append(tasks, crons);
    await Promise.all([listing(tasks, "tasks", capabilities.task_listing), capabilities.cron ? listing(crons, "crons", capabilities.cron_listing) : Promise.resolve(crons.append(node("p", "Configure cron storage to manage schedules.")))]);
    if (ticket !== generation) return;
    const fixed = section("Fixed schedules · source controlled");
    for (const item of capabilities.fixed) fixed.append(node("p", `${item.name} · ${item.expression} UTC · ${item.task_name}`));
    if (!capabilities.fixed.length) fixed.append(node("p", "No fixed schedules in this deployment."));
    fixed.append(node("p", "Edit fixed schedules in cron/, rebuild, and restart the agent.")); host.append(fixed);
  }

  function section(title) { const result = node("section"); result.className = "work-section"; result.append(node("h3", title)); return result; }

  /** Retain cursor pages independently and stop stale responses after navigation or scope changes. */
  async function listing(parent, resource, supported) {
    const ticket = generation, selectedScope = scope, rows = node("div"); parent.append(rows);
    const lookup = node("form"), id = node("input"); id.placeholder = resource === "tasks" ? "Task ID" : "Schedule ID"; id.setAttribute("aria-label", id.placeholder); id.required = true;
    const find = node("button", "Look up ID"); find.className = "secondary-button";
    lookup.append(id, find); parent.append(lookup);
    lookup.addEventListener("submit", event => { event.preventDefault(); runAction(async () => {
      const item = await request(`/${resource}/${encodeURIComponent(id.value)}`, "GET", undefined, selectedScope);
      if (ticket === generation) rows.replaceChildren(row(item, resource, selectedScope));
    }); });
    if (!supported) { rows.append(node("p", "This custom provider supports lookup by ID. Add its optional metadata listing capability to browse pages.")); return; }
    let after = null;
    const more = button("Load more", load); parent.append(more);
    async function load() {
      const response = await api(path(`/${resource}`, selectedScope) + (after ? `&after=${encodeURIComponent(after)}` : ""));
      const page = await response.json();
      if (ticket !== generation) return;
      if (page.indexing) { more.hidden = false; more.textContent = "Continue indexing existing tasks"; return; }
      for (const item of page.items) rows.append(row(item, resource, selectedScope));
      if (!page.items.length && !after) rows.append(node("p", "No work for this owner."));
      after = page.after; more.hidden = !after; more.textContent = "Load more";
    }
    await load();
  }

  function row(item, resource, selectedScope) {
    const card = node("article"); card.className = "work-card";
    const identity = item.job_id || item.schedule_id;
    card.append(node("strong", item.task_name), node("code", identity), node("p", resource === "tasks"
      ? `${item.status} · queue ${item.queue} · attempt ${item.attempt}/${item.max_retries + 1}`
      : `${item.key} · ${item.expression} UTC · ${item.status} · next ${new Date(item.next_run_at * 1000).toLocaleString()}`));
    const mutate = async (suffix, method, body) => { await request(`/${resource}/${encodeURIComponent(identity)}${suffix}`, method, body, selectedScope); await open(api); };
    if (resource === "tasks" && ["pending", "running"].includes(item.status)) card.append(button("Cancel task", () => mutate("/cancel", "POST")));
    if (resource === "crons" && !item.read_only) {
      if (item.status !== "cancelled") {
        card.append(button("Edit", () => editSchedule(identity, selectedScope)), button(item.status === "paused" ? "Resume" : "Pause", () => mutate("/status", "POST", {status:item.status === "paused" ? "active" : "paused"})), button("Cancel schedule", () => mutate("/status", "POST", {status:"cancelled"})));
      }
      card.append(button("Delete", async () => { if (window.confirm("Delete this schedule? Existing tasks will remain.")) await mutate("", "DELETE"); }));
    }
    return card;
  }

  /** Edit a fresh, revision-bound snapshot rather than overwriting a concurrent update. */
  async function editSchedule(identity, selectedScope = scope) {
    const record = identity ? await request(`/crons/${encodeURIComponent(identity)}`, "GET", undefined, selectedScope) : null;
    const dialog = node("dialog"), form = node("form"), error = node("p"); dialog.className = "work-dialog"; error.setAttribute("role", "alert");
    form.append(node("h3", identity ? "Edit schedule" : "Create schedule"));
    const key = field(form, "Idempotency key", record?.key || ""), target = node("select");
    key.disabled = Boolean(identity);
    const label = node("label", "Deployed cron function");
    for (const name of capabilities.targets) { const option = node("option", name); option.value = name; target.append(option); }
    target.disabled = Boolean(identity); label.append(target); if (!identity) form.append(label);
    const expression = field(form, "Schedule (UTC, five columns)", record?.expression || "0 9 * * *");
    const args = field(form, "Arguments (JSON object)", JSON.stringify(record?.arguments || {}, null, 2), true);
    const save = node("button", identity ? "Save schedule" : "Create schedule"); save.className = "secondary-button";
    form.append(error, save, button("Close", () => dialog.close())); dialog.append(form); document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove()); dialog.showModal();
    form.addEventListener("submit", async event => {
      event.preventDefault(); save.disabled = true;
      try {
        const argumentsValue = JSON.parse(args.value);
        await request(identity ? `/crons/${encodeURIComponent(identity)}` : "/crons", identity ? "PATCH" : "POST", identity
          ? {expression:expression.value, arguments:argumentsValue, revision:record.revision}
          : {key:key.value, task:target.value, expression:expression.value, arguments:argumentsValue}, selectedScope);
        dialog.close(); await open(api);
      } catch (failure) { error.textContent = failure instanceof SyntaxError ? "Arguments must be valid JSON." : failure.message; }
      finally { save.disabled = false; }
    });
  }
  function field(form, title, value, multiline = false) {
    const label = node("label", title), input = node(multiline ? "textarea" : "input"); input.value = value; input.required = true;
    if (multiline) input.rows = 6;
    label.append(input); form.append(label); return input;
  }
  return {open};
})();
