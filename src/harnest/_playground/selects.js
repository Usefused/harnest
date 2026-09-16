/* Enhance native selects while keeping their values, validation, and change events authoritative. */
const harnestSelects = (() => {
  const controls = new Map();
  let opened = null, serial = 0, search = "", searchAt = 0;
  const node = (tag, cls) => { const value = document.createElement(tag); value.className = cls; return value; };
  const options = select => [...select.options].map((item, index) => ({index, text: item.label, disabled: item.disabled || item.parentElement.disabled, selected: item.selected}));

  /** Arrow navigation skips unavailable options and stops at the list boundaries. */
  function nextIndex(items, current, direction) {
    let index = current + direction;
    while (index >= 0 && index < items.length) {
      if (!items[index].disabled) return index;
      index += direction;
    }
    return current;
  }
  function matchingIndex(items, current, text) {
    for (let offset = 1; offset <= items.length; offset++) {
      const index = (current + offset + items.length) % items.length;
      if (!items[index].disabled && items[index].text.toLocaleLowerCase().startsWith(text)) return index;
    }
    return current;
  }
  /** Fit the popup inside the viewport and prefer opening below the field. */
  function menuBounds(rect, viewport, count) {
    const width = Math.min(Math.max(rect.width, 200), viewport.width - 16);
    const desired = Math.min(count * 42 + 12, 280);
    const below = viewport.height - rect.bottom - 12, above = rect.top - 12;
    const upwards = below < desired && above > below;
    const height = Math.max(0, Math.min(desired, upwards ? above : below));
    return {left: Math.max(8, Math.min(rect.left, viewport.width - width - 8)), top: upwards ? rect.top - height - 6 : rect.bottom + 6, width, height};
  }
  function close() {
    if (!opened) return;
    const control = opened; opened = null;
    if (control.popup.matches(":popover-open")) control.popup.hidePopover();
    control.popup.hidden = true;
    control.trigger.setAttribute("aria-expanded", "false");
    control.trigger.removeAttribute("aria-activedescendant");
    search = "";
  }
  function highlight(control, index) {
    control.active = index;
    for (const row of control.popup.children) row.classList.toggle("is-active", Number(row.dataset.index) === index);
    const row = control.popup.children[index];
    if (!row) return;
    control.trigger.setAttribute("aria-activedescendant", row.id);
    const top = row.offsetTop, bottom = top + row.offsetHeight;
    if (top < control.popup.scrollTop) control.popup.scrollTop = top;
    else if (bottom > control.popup.scrollTop + control.popup.clientHeight) control.popup.scrollTop = bottom - control.popup.clientHeight;
  }
  function commit(control, index) {
    if (!control.select.options[index] || options(control.select)[index].disabled) return;
    const changed = control.select.selectedIndex !== index;
    control.select.selectedIndex = index;
    close(); sync(control); control.trigger.focus();
    if (changed) {
      control.select.dispatchEvent(new Event("input", {bubbles: true}));
      control.select.dispatchEvent(new Event("change", {bubbles: true}));
    }
  }
  function position(control) {
    const rect = control.trigger.getBoundingClientRect();
    const bounds = menuBounds(rect, {width: window.innerWidth, height: window.innerHeight}, control.select.options.length);
    for (const key of ["left", "top", "width"]) control.popup.style[key] = `${bounds[key]}px`;
    control.popup.style.maxHeight = `${bounds.height}px`;
  }
  /** Popovers stay above scrolling panels and inside modal focus ownership. */
  function open(control) {
    if (control.select.disabled || !control.select.options.length) return;
    close(); sync(control); opened = control;
    control.popup.replaceChildren();
    for (const item of options(control.select)) {
      const row = node("div", "harnest-select-option"); row.id = `${control.popup.id}-${item.index}`;
      row.dataset.index = item.index; row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(item.selected)); row.setAttribute("aria-disabled", String(Boolean(item.disabled)));
      const text = node("span", "harnest-select-option-text"); text.textContent = item.text;
      const check = node("span", "harnest-select-check"); check.textContent = item.selected ? "✓" : ""; check.setAttribute("aria-hidden", "true");
      row.append(text, check);
      row.addEventListener("pointermove", () => { if (!item.disabled) highlight(control, item.index); });
      row.addEventListener("pointerdown", event => event.preventDefault());
      row.addEventListener("click", () => commit(control, item.index));
      control.popup.append(row);
    }
    control.trigger.closest("dialog")?.append(control.popup);
    if (!control.popup.isConnected) document.body.append(control.popup);
    control.popup.hidden = false; control.popup.showPopover(); position(control);
    control.trigger.setAttribute("aria-expanded", "true");
    highlight(control, control.select.selectedIndex >= 0 ? control.select.selectedIndex : nextIndex(options(control.select), -1, 1));
  }
  function keydown(control, event) {
    const keys = ["ArrowDown", "ArrowUp", "Home", "End", "Enter", " ", "Escape"];
    if (event.key === "Tab") { close(); return; }
    if (event.key === "Escape") { if (opened === control) { event.preventDefault(); event.stopPropagation(); close(); } return; }
    if (keys.includes(event.key)) {
      event.preventDefault();
      if (opened !== control) { open(control); return; }
      if (event.key === "Enter" || event.key === " ") { commit(control, control.active); return; }
      const items = options(control.select), direction = ["ArrowUp", "End"].includes(event.key) ? -1 : 1;
      const start = event.key === "Home" ? -1 : event.key === "End" ? items.length : control.active;
      highlight(control, nextIndex(items, start, direction));
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      if (opened !== control) open(control);
      search = Date.now() - searchAt < 700 ? search + event.key.toLocaleLowerCase() : event.key.toLocaleLowerCase(); searchAt = Date.now();
      highlight(control, matchingIndex(options(control.select), control.active, search));
    }
  }
  function sync(control) {
    const text = control.select.selectedOptions[0]?.label || "Choose an option";
    if (control.caption.textContent !== text) control.caption.textContent = text;
    if (control.trigger.disabled !== control.select.disabled) control.trigger.disabled = control.select.disabled;
    control.trigger.setAttribute("aria-required", String(control.select.required));
  }
  function enhance(select) {
    if (controls.has(select) || select.multiple || select.size > 1 || document.getElementById(select.getAttribute("data-select-proxy"))) return;
    const wrapper = node("span", "harnest-select"), trigger = node("button", "harnest-select-trigger"), caption = node("span", "harnest-select-value");
    const popup = node("div", "harnest-select-menu harnest-option-menu"); popup.id = `harnest-options-${++serial}`; popup.popover = "manual"; popup.hidden = true;
    popup.setAttribute("role", "listbox");
    const name = select.getAttribute("aria-label") || [...select.labels].map(label => [...label.childNodes].filter(child => child.nodeType === 3).map(child => child.textContent).join("").trim()).join(" ") || "Choose an option";
    trigger.type = "button"; trigger.setAttribute("role", "combobox"); trigger.setAttribute("aria-label", name); trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false"); trigger.setAttribute("aria-controls", popup.id); popup.setAttribute("aria-label", name);
    const chevron = node("span", "harnest-select-chevron"); chevron.setAttribute("aria-hidden", "true"); trigger.append(caption, chevron);
    select.before(wrapper); wrapper.append(trigger);
    select.classList.add("harnest-select-native"); select.tabIndex = -1; select.setAttribute("aria-hidden", "true");
    const control = {select, trigger, caption, popup, wrapper, active: -1}; controls.set(select, control);
    trigger.addEventListener("click", event => { event.preventDefault(); opened === control ? close() : open(control); });
    trigger.addEventListener("keydown", event => keydown(control, event));
    select.addEventListener("change", () => sync(control));
    select.addEventListener("invalid", event => { event.preventDefault(); trigger.focus(); open(control); });
    select.addEventListener("focus", () => trigger.focus());
    sync(control);
    if (document.activeElement === select) trigger.focus();
  }
  function refresh() {
    for (const select of document.querySelectorAll("select")) enhance(select);
    for (const [select, control] of controls) {
      if (!select.isConnected) { if (opened === control) close(); control.popup.remove(); controls.delete(select); }
      else sync(control);
    }
  }
  function start() {
    // Retain a fully functional native fallback in browsers without the Popover API.
    if (typeof HTMLElement.prototype.showPopover !== "function") return;
    refresh();
    new MutationObserver(refresh).observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["disabled", "selected", "label"]});
    document.addEventListener("pointerdown", event => { if (opened && !opened.wrapper.contains(event.target) && !opened.popup.contains(event.target)) close(); });
    document.addEventListener("scroll", event => { if (opened && !opened.popup.contains(event.target)) position(opened); }, true);
    document.addEventListener("close", close, true);
    window.addEventListener("resize", close);
  }
  return {start, nextIndex, matchingIndex, menuBounds};
})();
if (typeof document !== "undefined") harnestSelects.start();
