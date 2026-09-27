/**
 * The 40 lines of DOM helper that stand in for a framework.
 *
 * Everything user-visible is built with `el()` rather than innerHTML, so no
 * value that came off the wire is ever parsed as markup. That is the whole
 * XSS story for this site: there is no `innerHTML` sink to misuse.
 */

/**
 * Create an element.
 *
 * @param {string} tag
 * @param {object} [props] attributes; `class`, `text`, `dataset`,
 *   `on` (event map) and `aria*` are handled specially.
 * @param {Array<Node|string|null|undefined|false>} [children]
 * @returns {HTMLElement}
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "on") {
      for (const [type, fn] of Object.entries(value)) node.addEventListener(type, fn);
    } else if (key === "value") node.value = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Append a possibly-nested list of children, skipping falsy entries. */
export function append(parent, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false || child === "") continue;
    if (Array.isArray(child)) append(parent, child);
    else parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

/** Replace a node's children. */
export function replace(parent, children) {
  if (!parent) return parent;
  parent.replaceChildren();
  return append(parent, children);
}

/** `document.querySelector`, but shorter and null-tolerant at call sites. */
export function $(selector, root = document) {
  return root.querySelector(selector);
}

/** `document.querySelectorAll` as a real array. */
export function $$(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

/** A `<span class="badge badge-...">` for a confidence tier or free-form label. */
export function badge(label, variant) {
  const cls = variant ? `badge badge-${variant}` : "badge";
  return el("span", { class: cls, text: label });
}

/** Empty-state placeholder. */
export function emptyState(message) {
  return el("p", { class: "empty", text: message });
}

/**
 * Write a status line into a `<p class="status">` region.
 *
 * @param {HTMLElement|null} node
 * @param {string} message empty string hides the region
 * @param {"info"|"working"|"error"} [kind]
 */
export function setStatus(node, message, kind = "info") {
  if (!node) return;
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.dataset.kind = kind;
  node.textContent = message;
}

/** Format a USDC price the way the pricing table and buttons both want it. */
export function formatUsdc(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  return `$${num.toFixed(2)}`;
}

/** Format a large count with thousands separators. */
export function formatCount(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  return num.toLocaleString();
}

/**
 * Render an ISO timestamp in the reader's locale, falling back to the raw
 * string when the API sends something Date cannot parse.
 */
export function formatTimestamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/** Percent helper for 0–1 shares that are sometimes already 0–100. */
export function formatShare(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  const pct = num <= 1 ? num * 100 : num;
  return `${pct.toFixed(1)}%`;
}

/** Copy text to the clipboard, falling back to a hidden textarea. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = el("textarea", { value: text, "aria-hidden": "true" });
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}
