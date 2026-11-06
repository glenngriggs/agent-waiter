/*
 * AgentWaiter web client: plain JavaScript, no framework, no build step, no CDN.
 *
 * It talks only to the AgentWaiter backend (docs/designs/agent-platform.md, sections 4.1 and 4.3).
 * Rules this file keeps everywhere:
 *   - The DOM is built with createElement + textContent (the h() helper below). innerHTML is
 *     never used, so text written by agents, files or other users cannot inject markup or script.
 *   - Every request sends `X-Den-CSRF: 1`; the backend rejects non-GET /api requests without it.
 *   - Links built from server data are always "#/..." hashes or /api URLs with encoded parameters.
 *
 * Layout of this file:
 *   1. DOM helpers and icons        6. Agent session (one EventSource per open agent)
 *   2. Formatting                   7. Timeline renderer (chat and job-run transcripts)
 *   3. API client and toasts        8. Agent page: header, settings, tabs
 *   4. App state and router         9. Chat tab / 10. Files tab / 11. Jobs tab
 *   5. Auth, agent list, onboarding 12. Settings page (API keys, usage, account)
 *                                   13. Boot
 */
(function () {
  "use strict";

  const HISTORY_LIMIT = 300;
  const COMMAND_OUTPUT_KEEP = 20000; // characters of live command output kept per command row

  // ======================================================================
  // 1. DOM helpers and icons
  // ======================================================================

  // Keys that must be set as DOM properties rather than attributes (for example a
  // textarea's value or a checkbox's checked state).
  const DOM_PROPS = new Set(["value", "checked", "disabled", "hidden", "selected", "multiple", "required", "open", "readOnly"]);

  /** Create an element. props: class, text, dataset, style (object), on<event>, properties, attributes. */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    if (props) {
      for (const key of Object.keys(props)) {
        const value = props[key];
        if (value === null || value === undefined || value === false) continue;
        if (key === "innerHTML" || key === "outerHTML") throw new Error("h(): HTML strings are not allowed");
        if (key === "class") el.className = value;
        else if (key === "text") el.textContent = String(value);
        else if (key === "dataset") Object.assign(el.dataset, value);
        else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
        else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
        else if (DOM_PROPS.has(key)) el[key] = value;
        else el.setAttribute(key, value === true ? "" : String(value));
      }
    }
    appendAll(el, children);
    return el;
  }

  /**
   * Replace an element's children. Use this instead of el.replaceChildren(...), which turns
   * null into the text "null"; this skips null/false like h() does and flattens arrays.
   */
  function setChildren(el, ...children) {
    el.replaceChildren();
    return appendAll(el, children);
  }

  function appendAll(parent, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false || child === "") continue;
      if (Array.isArray(child)) appendAll(parent, child);
      else if (child instanceof Node) parent.appendChild(child);
      else parent.appendChild(document.createTextNode(String(child)));
    }
    return parent;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  // Static path data only (24x24, stroked). Never built from server data.
  const ICONS = {
    plus: "M12 5v14M5 12h14",
    sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
    moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
    x: "M6 6l12 12M18 6L6 18",
    check: "M5 12l5 5 9-10",
    back: "M15 18l-6-6 6-6",
    send: "M4 12l16-8-6 16-2-6z",
    stop: "M7 7h10v10H7z",
    play: "M8 5l11 7-11 7z",
    restart: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
    settings: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4",
    folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
    file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
    download: "M12 4v12M7 11l5 5 5-5M4 20h16",
    trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
    terminal: "M4 17l6-5-6-5M12 19h8",
    box: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5",
    clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
    alert: "M12 3l10 18H2zM12 10v4M12 17.5v.5",
    search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
    note: "M5 6h14M5 11h14M5 16h9",
    refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
    chevron: "M9 6l6 6-6 6",
    gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z",
    external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  };

  function icon(name, extraClass) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon" + (extraClass ? " " + extraClass : ""));
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[name] || ICONS.box);
    svg.appendChild(path);
    return svg;
  }

  function spinner() {
    return h("span", { class: "spinner", "aria-hidden": "true" });
  }

  function badge(text, tone, withSpinner) {
    return h("span", { class: `badge badge-${tone || "muted"}` }, withSpinner ? spinner() : null, text);
  }

  const STATUS_TONES = {
    active: "ok", succeeded: "ok", completed: "ok", ready: "ok",
    running: "info", queued: "info", creating: "info", starting: "info", restarting: "info",
    failed: "danger", error: "danger",
    interrupted: "warn", skipped: "warn", missed: "warn",
    cancelled: "muted", finished: "muted", stopped: "muted",
  };
  const STATUS_LABELS = {
    active: "Active", cancelled: "Cancelled", finished: "Finished",
    queued: "Queued", running: "Running", succeeded: "Succeeded", failed: "Failed",
    interrupted: "Interrupted", skipped: "Skipped", missed: "Missed",
  };

  function statusBadge(status) {
    const busy = status === "running" || status === "queued";
    return badge(STATUS_LABELS[status] || status || "unknown", STATUS_TONES[status] || "muted", busy);
  }

  function containerBadge(container) {
    const map = {
      running: ["Container running", "ok"],
      stopped: ["Container stopped", "muted"],
      missing: ["No container yet", "warn"],
      unknown: ["Container status unknown", "warn"],
      starting: ["Container starting", "info"],
      restarting: ["Container restarting", "info"],
    };
    const [label, tone] = map[container] || [String(container || "unknown"), "muted"];
    return badge(label, tone, container === "starting" || container === "restarting");
  }

  function field(label, control, hint) {
    return h("label", { class: "field" },
      h("span", { class: "field-label", text: label }),
      control,
      hint ? h("span", { class: "field-hint", text: hint }) : null);
  }

  function loadingRow(text) {
    return h("div", { class: "loading" }, spinner(), h("span", { text: text || "Loading…" }));
  }

  function errorBlock(message, action) {
    return h("div", { class: "notice notice-error", role: "alert" }, icon("alert"), h("span", { text: message }), action || null);
  }

  function emptyBlock(text) {
    return h("div", { class: "empty" }, h("p", { text }));
  }

  /** Disable a button while an async action runs, so it cannot be pressed twice. */
  async function withBusy(button, fn) {
    if (button) { button.disabled = true; button.classList.add("is-busy"); }
    try {
      return await fn();
    } finally {
      if (button) { button.disabled = false; button.classList.remove("is-busy"); }
    }
  }

  function avatar(name, id) {
    // Any hue, picked from the agent's id so it stays the same across visits.
    // style.css (.avatar-tint) turns the hue into a pale fill and a darker letter.
    let hash = 0;
    for (const ch of String(id || name)) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    const initial = (String(name || "?").trim()[0] || "?").toUpperCase();
    return h("span", { class: "avatar avatar-tint", style: `--hue: ${(hash * 137) % 360}`, text: initial, "aria-hidden": "true" });
  }

  // ======================================================================
  // 2. Formatting
  // ======================================================================

  function parseTime(value) {
    if (value === null || value === undefined || value === "") return null;
    let date;
    if (typeof value === "number") date = new Date(value < 1e12 ? value * 1000 : value);
    // The backend writes Python microseconds; trim to milliseconds so every browser parses it.
    else date = new Date(String(value).replace(/(\.\d{3})\d+/, "$1"));
    return isNaN(date.getTime()) ? null : date;
  }

  function fmtDateTime(value) {
    const d = parseTime(value);
    return d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
  }

  function fmtClock(value) {
    const d = parseTime(value);
    if (!d) return "";
    const sameDay = d.toDateString() === new Date().toDateString();
    return sameDay
      ? d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
      : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  const relativeFormat = typeof Intl.RelativeTimeFormat === "function"
    ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
    : null;

  function fmtRelative(value) {
    const d = parseTime(value);
    if (!d) return "";
    const diffSec = (d.getTime() - Date.now()) / 1000;
    const abs = Math.abs(diffSec);
    if (abs < 45) return diffSec >= 0 ? "in a moment" : "just now";
    if (!relativeFormat) return fmtDateTime(value);
    const units = [["minute", 60], ["hour", 3600], ["day", 86400], ["month", 2592000], ["year", 31536000]];
    let unit = "minute";
    let size = 60;
    for (const [u, s] of units) if (abs >= s) { unit = u; size = s; }
    return relativeFormat.format(Math.round(diffSec / size), unit);
  }

  function timeEl(value) {
    return h("time", { datetime: String(value || ""), title: fmtDateTime(value), text: fmtRelative(value) });
  }

  function fmtSize(bytes) {
    if (typeof bytes !== "number" || bytes < 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let n = bytes / 1024;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
  }

  function fmtDuration(ms) {
    if (typeof ms !== "number" || ms < 0) return "";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
    const min = Math.floor(ms / 60000);
    const sec = Math.round((ms % 60000) / 1000);
    return sec ? `${min} min ${sec} s` : `${min} min`;
  }

  function between(startIso, endIso) {
    const a = parseTime(startIso);
    const b = parseTime(endIso);
    return a && b ? fmtDuration(b.getTime() - a.getTime()) : "";
  }

  function userTimezone() {
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz) return tz;
    } catch (err) {
      console.warn("Could not read the browser's timezone", err);
    }
    return (state.user && state.user.timezone) || "UTC";
  }

  function prettyJson(text) {
    if (typeof text !== "string") return text === undefined || text === null ? "" : JSON.stringify(text, null, 2);
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch (err) {
      return text; // not JSON (or truncated at 2000 chars): show it as sent
    }
  }

  function errorText(error) {
    if (!error) return "";
    if (typeof error === "string") return error;
    if (error.message) return String(error.message);
    try { return JSON.stringify(error); } catch (err) { return String(error); }
  }

  // ---- workspace paths --------------------------------------------------

  function basename(path) {
    const parts = String(path || "").split("/").filter(Boolean);
    return parts.length ? parts[parts.length - 1] : "";
  }

  function dirname(path) {
    const parts = String(path || "").split("/").filter(Boolean);
    parts.pop();
    return parts.join("/");
  }

  function joinPath(dir, name) {
    return dir ? `${dir}/${name}` : name;
  }

  /**
   * Turn a path the agent reported into a workspace-relative path, or null if it is outside.
   * Docker agents report /workspace/...; the local runtime reports the host path
   * .../agents/<id>/workspace/....
   */
  function workspaceRelative(path) {
    if (typeof path !== "string" || !path) return null;
    if (!path.startsWith("/")) return path.replace(/^\.\//, "");
    const m = path.match(/^(?:.*\/agents\/[0-9a-f]{32})?\/workspace(?:\/(.*))?$/);
    return m ? (m[1] || "").replace(/\/+$/, "") : null;
  }

  // ======================================================================
  // 3. API client and toasts
  // ======================================================================

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.body = body;
    }
  }

  function detailText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // FastAPI validation errors look like [{loc: ["body", "name"], msg: "..."}].
      return detail.map((d) => {
        if (!d || !d.msg) return String(d);
        const where = Array.isArray(d.loc) ? d.loc.slice(1).join(".") : "";
        return where ? `${where}: ${d.msg}` : d.msg;
      }).join("; ");
    }
    try { return JSON.stringify(detail); } catch (err) { return String(detail); }
  }

  /**
   * Call the backend. Sends JSON (or FormData as-is), always with the CSRF header and the
   * session cookie. Non-2xx answers become ApiError carrying the server's `detail`.
   * A 401 sends the user to the login page unless opts.allow401 is set.
   */
  async function api(method, path, body, opts) {
    const options = opts || {};
    const headers = { "X-Den-CSRF": "1", Accept: "application/json" };
    let payload;
    if (body instanceof FormData) {
      payload = body; // the browser sets the multipart boundary itself
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
    } catch (err) {
      console.error(`${method} ${path} failed`, err);
      throw new ApiError("Could not reach the AgentWaiter server. Check that it is running, then try again.", 0, null);
    }
    let data = null;
    if (res.status !== 204 && (res.headers.get("content-type") || "").includes("application/json")) {
      try {
        data = await res.json();
      } catch (err) {
        console.warn(`${method} ${path}: the response was not valid JSON`, err);
      }
    }
    if (!res.ok) {
      if (res.status === 401 && !options.allow401) handleSignedOut();
      const message = data && data.detail !== undefined
        ? detailText(data.detail)
        : `The server answered ${res.status}${res.statusText ? " " + res.statusText : ""}.`;
      throw new ApiError(message, res.status, data);
    }
    return data;
  }

  function enc(value) {
    return encodeURIComponent(String(value));
  }

  function agentUrl(agentId, suffix) {
    return `/api/agents/${enc(agentId)}${suffix || ""}`;
  }

  function withQuery(path, params) {
    const q = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null) q.set(key, String(value));
    }
    const s = q.toString();
    return s ? `${path}?${s}` : path;
  }

  function toast(message, kind) {
    const tone = kind || "error";
    const box = document.getElementById("toasts");
    const el = h("div", { class: `toast toast-${tone}`, role: tone === "error" ? "alert" : "status" },
      h("span", { class: "toast-text", text: message }),
      h("button", { class: "toast-close", type: "button", "aria-label": "Dismiss", onclick: () => el.remove() }, icon("x")));
    box.appendChild(el);
    setTimeout(() => el.remove(), tone === "error" ? 9000 : 4000);
  }

  function showError(err) {
    if (err && err.status === 401) return; // already redirected to the login page
    console.error(err);
    toast(err && err.message ? err.message : String(err), "error");
  }

  // ======================================================================
  // 4. App state and router
  // ======================================================================

  const state = {
    config: null,    // GET /api/config
    user: null,      // {id, email, timezone} or null when signed out
    providers: null, // GET /api/providers (model providers and the user's keys); null until loaded
    session: null,   // AgentSession for the agent whose page is open
    cleanups: [],    // run when the current view is replaced
    routeToken: 0,   // bumps on every navigation so late async renders can tell they are stale
  };

  function parseHash() {
    const raw = location.hash.replace(/^#/, "");
    const qi = raw.indexOf("?");
    const pathPart = qi >= 0 ? raw.slice(0, qi) : raw;
    const query = new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : "");
    const parts = pathPart.split("/").filter(Boolean).map((p) => {
      try { return decodeURIComponent(p); } catch (err) { return p; }
    });
    return { parts, query };
  }

  function go(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  }

  /** Navigate without adding a history entry (used for redirects). */
  function redirect(hash) {
    if (location.hash === hash) route();
    else location.replace(hash);
  }

  function onUnmount(fn) {
    state.cleanups.push(fn);
  }

  function isCurrent(token) {
    return token === state.routeToken;
  }

  function closeSession() {
    if (state.session) {
      state.session.close();
      state.session = null;
    }
  }

  function ensureSession(agentId) {
    if (!state.session || state.session.agentId !== agentId || state.session.failed) {
      closeSession();
      state.session = new AgentSession(agentId);
    }
    return state.session;
  }

  function handleSignedOut() {
    state.user = null;
    state.providers = null;
    closeSession();
    renderUserBox();
    const page = parseHash().parts[0];
    if (page !== "login" && page !== "register") redirect("#/login");
  }

  function signedIn(user) {
    state.user = user;
    state.providers = null;
    renderUserBox();
    go("#/agents");
  }

  /**
   * The user's model providers (GET /api/providers). Cached in state.providers; pass
   * fresh=true where the answer must be current (pickers, the API keys page).
   */
  async function loadProviders(fresh) {
    if (!fresh && state.providers) return state.providers;
    const res = await api("GET", "/api/providers");
    state.providers = (res && res.providers) || [];
    return state.providers;
  }

  /** Replace one provider in the cache with the server's newer copy. */
  function storeProvider(provider) {
    if (!provider || !state.providers) return;
    state.providers = state.providers.map((p) => (p.id === provider.id ? provider : p));
  }

  /** A model's display name, from the providers list when it is loaded. */
  function modelLabel(modelId, providerId) {
    for (const p of state.providers || []) {
      if (providerId && p.id !== providerId) continue;
      const m = (p.models || []).find((x) => x.id === modelId);
      if (m) return m.label;
    }
    const m = ((state.config && state.config.models) || []).find((x) => x.id === modelId);
    return m ? m.label : String(modelId || "");
  }

  /** "Claude Sonnet 5.5 · Anthropic": the model and the provider whose key runs it. */
  function agentModelText(a) {
    const label = modelLabel(a.model, a.provider);
    return a.provider_label ? `${label} · ${a.provider_label}` : label;
  }

  /** True when a failure is fixed on the Settings page (a missing or rejected key). */
  function needsSettings(kind, message) {
    return kind === "no_api_key" || kind === "unauthorized" || /\bSettings\b/.test(String(message || ""));
  }

  /** A link to the Settings page styled as a button; `big` drops btn-sm to sit next to full-size buttons. */
  function settingsButton(label, big) {
    return h("a", { class: big ? "btn" : "btn btn-sm", href: "#/settings" }, icon("gear"), label || "Open Settings");
  }

  function limits() {
    return (state.config && state.config.limits) || {};
  }

  function route() {
    const cleanups = state.cleanups;
    state.cleanups = [];
    for (const fn of cleanups) {
      try { fn(); } catch (err) { console.error("View cleanup failed", err); }
    }
    const token = ++state.routeToken;
    const { parts, query } = parseHash();
    const page = parts[0] || "agents";
    const isPublic = page === "login" || page === "register";

    if (!state.user && !isPublic) { redirect("#/login"); return; }
    if (state.user && isPublic) { redirect("#/agents"); return; }

    // One EventSource per open agent: leaving the agent's pages closes its stream.
    const agentId = page === "agent" ? parts[1] : null;
    if (state.session && state.session.agentId !== agentId) closeSession();

    const root = document.getElementById("app");
    root.replaceChildren();
    document.title = "AgentWaiter";
    const settingsLink = document.getElementById("settings-link");
    if (page === "settings") settingsLink.setAttribute("aria-current", "page");
    else settingsLink.removeAttribute("aria-current");

    let view;
    if (page === "login") view = viewLogin(root);
    else if (page === "register") view = viewRegister(root);
    else if (page === "agents") view = viewAgents(root, token);
    else if (page === "new") view = viewNewAgent(root, token);
    else if (page === "settings" && (!parts[1] || SETTINGS_TABS.some(([key]) => key === parts[1]))) {
      view = viewSettings(root, token, parts[1] || "keys");
    }
    else if (page === "agent" && agentId) {
      const tab = parts[2] || "chat";
      if (["chat", "files", "jobs"].includes(tab)) view = viewAgent(root, token, agentId, tab, query);
      else viewNotFound(root);
    } else viewNotFound(root);
    Promise.resolve(view).catch(showError);
  }

  function viewNotFound(root) {
    root.appendChild(h("div", { class: "card narrow center" },
      h("h1", { text: "Page not found" }),
      h("p", { class: "muted", text: "That address does not match anything in AgentWaiter." }),
      h("a", { class: "btn btn-primary", href: "#/agents", text: "Go to your agents" })));
  }

  // ---- header ------------------------------------------------------------

  function renderUserBox() {
    const box = document.getElementById("user-box");
    box.hidden = !state.user;
    document.getElementById("user-email").textContent = state.user ? state.user.email : "";
  }

  async function logout() {
    try {
      await api("POST", "/api/auth/logout", undefined, { allow401: true });
    } catch (err) {
      if (err.status !== 401) showError(err);
    }
    state.user = null;
    state.providers = null;
    closeSession();
    renderUserBox();
    go("#/login");
  }

  // ======================================================================
  // 5. Auth, agent list and onboarding
  // ======================================================================

  function authShell(title, subtitle, form, footer) {
    return h("div", { class: "auth-wrap" },
      h("div", { class: "card auth-card" },
        h("h1", { text: title }),
        h("p", { class: "muted", text: subtitle }),
        form,
        footer));
  }

  function viewLogin(root) {
    document.title = "Sign in · AgentWaiter";
    const email = h("input", { type: "email", name: "email", autocomplete: "username", required: true });
    const password = h("input", { type: "password", name: "password", autocomplete: "current-password", required: true });
    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const submit = h("button", { class: "btn btn-primary btn-block", type: "submit", text: "Sign in" });
    const form = h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        error.hidden = true;
        withBusy(submit, async () => {
          try {
            const res = await api("POST", "/api/auth/login", { email: email.value.trim(), password: password.value }, { allow401: true });
            signedIn(res.user);
          } catch (err) {
            error.textContent = err.message;
            error.hidden = false;
          }
        });
      },
    }, field("Email", email), field("Password", password), error, submit);
    root.appendChild(authShell("Sign in", "Use the email and password you registered with.", form,
      h("p", { class: "auth-switch" }, "No account yet? ", h("a", { href: "#/register", text: "Create an account" }))));
    email.focus();
  }

  function viewRegister(root) {
    document.title = "Create account · AgentWaiter";
    const tz = userTimezone();
    const email = h("input", { type: "email", name: "email", autocomplete: "username", required: true });
    const password = h("input", { type: "password", name: "password", autocomplete: "new-password", required: true, minlength: 8, maxlength: 200 });
    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const submit = h("button", { class: "btn btn-primary btn-block", type: "submit", text: "Create account" });
    const form = h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        error.hidden = true;
        withBusy(submit, async () => {
          try {
            const res = await api("POST", "/api/auth/register",
              { email: email.value.trim(), password: password.value, timezone: tz }, { allow401: true });
            signedIn(res.user);
          } catch (err) {
            error.textContent = err.message;
            error.hidden = false;
          }
        });
      },
    },
    field("Email", email),
    field("Password", password, "At least 8 characters."),
    h("p", { class: "muted small", text: `Your timezone is ${tz}. Job schedules use it.` }),
    error, submit);
    root.appendChild(authShell("Create an account", "You can set up an agent once you are signed in.", form,
      h("p", { class: "auth-switch" }, "Already have an account? ", h("a", { href: "#/login", text: "Sign in" }))));
    email.focus();
  }

  // ---- agents list ------------------------------------------------------

  async function viewAgents(root, token) {
    document.title = "Agents · AgentWaiter";
    const list = h("div", { class: "agent-grid" }, loadingRow("Loading your agents…"));
    const newBtn = h("a", { class: "btn btn-primary", href: "#/new" }, icon("plus"), "New agent");
    const limitNote = h("p", { class: "muted small", hidden: true });
    root.append(
      h("div", { class: "page-head" },
        h("div", null,
          h("h1", { text: "Your agents" }),
          h("p", { class: "muted", text: "Each agent has its own computer, files, conversation and schedule." })),
        h("div", { class: "page-head-actions" }, newBtn, limitNote)),
      list);

    let pollTimer = null;
    onUnmount(() => clearTimeout(pollTimer));

    const load = async () => {
      let res;
      try {
        // Providers only improve the model labels; the list shows without them.
        [res] = await Promise.all([api("GET", "/api/agents"), loadProviders().catch(() => null)]);
      } catch (err) {
        if (isCurrent(token)) setChildren(list, errorBlock(err.message));
        return;
      }
      if (!isCurrent(token)) return;
      const agents = res.agents || [];
      const max = limits().max_agents_per_user;
      const atLimit = typeof max === "number" && agents.length >= max;
      newBtn.classList.toggle("disabled", atLimit);
      if (atLimit) {
        newBtn.removeAttribute("href");
        newBtn.setAttribute("aria-disabled", "true");
        limitNote.textContent = `You have the maximum of ${max} agents. Delete one to create another.`;
      } else {
        newBtn.setAttribute("href", "#/new");
        newBtn.removeAttribute("aria-disabled");
      }
      limitNote.hidden = !atLimit;
      if (!agents.length) {
        setChildren(list, h("div", { class: "card empty-hero" },
          h("h2", { text: "No agents yet" }),
          h("p", { class: "muted", text: "Create an agent to get started. Each one runs in its own container and keeps working when you close the tab." }),
          h("a", { class: "btn btn-primary", href: "#/new" }, icon("plus"), "New agent")));
      } else {
        setChildren(list, ...agents.map(agentCard));
      }
      // Keep the cards current while any agent is still being set up.
      if (agents.some((a) => a.setup_state === "creating")) pollTimer = setTimeout(load, 3000);
    };
    await load();
  }

  function agentCard(a) {
    let setup = null;
    if (a.setup_state === "creating") setup = badge("Setting up", "info", true);
    else if (a.setup_state === "error") setup = badge("Setup failed", "danger");
    return h("a", { class: "card agent-card", href: `#/agent/${enc(a.id)}/chat` },
      h("div", { class: "agent-card-top" },
        avatar(a.name, a.id),
        h("div", { class: "agent-card-title" },
          h("h2", { text: a.name }),
          h("span", { class: "muted small", text: agentModelText(a) }))),
      h("div", { class: "badges" },
        setup,
        a.setup_state === "ready" ? containerBadge(a.container) : null,
        a.chat_busy ? badge("Working", "info", true) : null),
      h("div", { class: "muted small" }, "Last active ", timeEl(a.last_active_at)));
  }

  // ---- model picker (onboarding, retry) ----------------------------------

  /**
   * Providers that offer at least one model, the ones that can run now first
   * (a stable sort, so the server's order holds within each half).
   */
  function pickerGroups(providers) {
    const withModels = (providers || []).filter((p) => (p.models || []).length);
    return withModels.filter((p) => p.available).concat(withModels.filter((p) => !p.available));
  }

  /**
   * Radio buttons for every model the user may pick, grouped under their provider.
   * Models of providers without a usable key are shown disabled, with a link to Settings.
   * `selected` is {provider, model}; when it cannot run, the first model that can is picked.
   */
  function modelPicker(providers, selected, groupName) {
    const radios = [];
    const groups = pickerGroups(providers).map((p) => h("div", { class: "model-group" + (p.available ? "" : " is-unavailable") },
      h("div", { class: "model-group-head" },
        h("span", { class: "model-group-label", text: p.label }),
        p.available ? null : h("a", { class: "model-group-action", href: "#/settings", text: "Add a key in Settings" })),
      h("div", { class: "model-options" }, p.models.map((m) => {
        const radio = h("input", { type: "radio", name: groupName, disabled: !p.available, dataset: { provider: p.id, model: m.id } });
        radios.push(radio);
        return h("label", { class: "model-option" + (p.available ? "" : " is-disabled") }, radio,
          h("span", { class: "model-option-body" },
            h("span", { class: "model-option-label", text: m.label }),
            h("span", { class: "model-option-meta" },
              h("span", { class: "model-option-id", text: m.id }),
              m.note ? h("span", { class: "model-option-note", text: m.note }) : null)));
      }))));
    const usable = radios.filter((r) => !r.disabled);
    const pick = usable.find((r) => selected && r.dataset.provider === selected.provider && r.dataset.model === selected.model) || usable[0];
    if (pick) pick.checked = true;
    return {
      el: h("div", { class: "model-picker", role: "radiogroup" }, groups),
      hasChoice: usable.length > 0,
      value: () => {
        const r = radios.find((x) => x.checked);
        return r ? { provider: r.dataset.provider, model: r.dataset.model } : null;
      },
    };
  }

  // ---- new agent ----------------------------------------------------------

  async function viewNewAgent(root, token) {
    document.title = "New agent · AgentWaiter";
    const card = h("div", { class: "card narrow" }, loadingRow("Loading models…"));
    root.appendChild(card);
    let providers;
    try {
      providers = await loadProviders(true);
    } catch (err) {
      if (isCurrent(token)) setChildren(card, errorBlock(err.message));
      return;
    }
    if (!isCurrent(token)) return;

    const picker = modelPicker(providers, null, "new-agent-model");
    if (!picker.hasChoice) {
      card.classList.add("center");
      setChildren(card,
        h("h1", { text: "Add an API key to create an agent" }),
        h("p", { class: "muted", text: "Agents think with a model from a provider such as Anthropic or OpenAI. Add your key for one in Settings, then come back here." }),
        h("div", { class: "form-actions" },
          h("a", { class: "btn btn-ghost", href: "#/agents", text: "Cancel" }),
          h("a", { class: "btn btn-primary", href: "#/settings" }, icon("gear"), "Open Settings")));
      return;
    }

    const name = h("input", { type: "text", maxlength: 40, required: true, placeholder: "e.g. Research helper", autocomplete: "off" });
    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Create agent");
    const form = h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        error.hidden = true;
        const trimmed = name.value.trim();
        if (!trimmed) { error.textContent = "Give your agent a name."; error.hidden = false; return; }
        const choice = picker.value();
        if (!choice) { error.textContent = "Pick a model."; error.hidden = false; return; }
        withBusy(submit, async () => {
          let res;
          try {
            res = await api("POST", "/api/agents", { name: trimmed, model: choice.model, provider: choice.provider });
          } catch (err) {
            error.textContent = err.message;
            error.hidden = false;
            return;
          }
          if (isCurrent(token)) showProgress(res.agent);
        });
      },
    },
    field("Name", name, "Up to 40 characters. You can rename it later."),
    h("div", { class: "field" }, h("span", { class: "field-label", text: "Model" }), picker.el,
      h("span", { class: "field-hint", text: "The model it thinks with, and whose key pays for it. You can switch later." })),
    error,
    h("div", { class: "form-actions" }, h("a", { class: "btn btn-ghost", href: "#/agents", text: "Cancel" }), submit));

    setChildren(card,
      h("h1", { text: "Create an agent" }),
      h("p", { class: "muted", text: "Pick a name and a model. The agent gets its own container and a workspace for files." }),
      form);
    name.focus();

    // After creation the same card becomes the progress list, fed by the agent's event stream.
    function showProgress(agent) {
      const host = h("div");
      setChildren(card, 
        h("h1", { text: `Setting up ${agent.name}` }),
        h("p", { class: "muted", text: "This usually takes under a minute. Setup keeps going if you leave this page." }),
        host);
      const session = ensureSession(agent.id);
      session.ready.then(() => {
        if (isCurrent(token)) mountSetupProgress(host, session);
      }).catch((err) => {
        if (isCurrent(token)) setChildren(host, errorBlock(err.message));
      });
    }
  }

  // ---- setup progress (onboarding screen and agents that are not ready) ---

  const SETUP_STEPS = [
    ["files", "Create the agent's files"],
    ["image", "Prepare the agent image"],
    ["container", "Start its container"],
    ["probe", "Check that the model answers"],
  ];

  function mountSetupProgress(host, session) {
    const rows = new Map();
    const list = h("ol", { class: "steps" });
    for (const [step, label] of SETUP_STEPS) {
      const msg = h("div", { class: "step-msg" });
      const li = h("li", { class: "step", dataset: { state: "pending" } },
        h("span", { class: "step-dot", "aria-hidden": "true" }),
        h("div", { class: "step-body" }, h("div", { class: "step-label", text: label }), msg));
      rows.set(step, { li, msg });
      list.appendChild(li);
    }
    const errorBox = h("div", { class: "setup-error", hidden: true });
    const readyBox = h("div", { class: "setup-ready", hidden: true },
      h("p", { class: "setup-ready-text" }, icon("check"), "Your agent is ready."),
      h("button", { class: "btn btn-primary", type: "button", onclick: () => go(`#/agent/${enc(session.agentId)}/chat`) }, "Open chat"));
    setChildren(host, list, errorBox, readyBox);

    let showingError = false;
    const setRow = (step, rowState, message) => {
      const row = rows.get(step);
      if (!row) return;
      row.li.dataset.state = rowState;
      if (message !== undefined) row.msg.textContent = message || "";
    };
    const markUpTo = (step, rowState, message) => {
      const idx = SETUP_STEPS.findIndex(([s]) => s === step);
      SETUP_STEPS.forEach(([s], i) => {
        if (i < idx) setRow(s, "done");
        else if (i === idx) setRow(s, rowState, message);
      });
    };
    const reset = () => {
      for (const [s] of SETUP_STEPS) setRow(s, "pending", "");
      errorBox.hidden = true;
      readyBox.hidden = true;
      showingError = false;
    };

    function showFailure(step, message) {
      showingError = true;
      readyBox.hidden = true;
      const stepLabel = (SETUP_STEPS.find(([s]) => s === step) || [null, null])[1];
      const current = session.agent ? { provider: session.agent.provider, model: session.agent.model } : null;
      // The picker needs the providers list; until it arrives (or if it fails) Retry keeps the model.
      let picker = null;
      const pickerHost = h("div", { class: "field" }, h("span", { class: "field-label", text: "Try again with model" }), loadingRow("Loading models…"));
      loadProviders(true).then((providers) => {
        picker = modelPicker(providers, current, `retry-model-${session.agentId}`);
        setChildren(pickerHost, h("span", { class: "field-label", text: "Try again with model" }),
          picker.hasChoice ? picker.el : h("p", { class: "muted small" }, "No model can run yet. ", h("a", { href: "#/settings", text: "Add an API key in Settings" }), ", then retry."));
      }).catch((err) => {
        console.warn("Could not load providers for the retry picker", err);
        pickerHost.hidden = true;
      });
      const retry = h("button", { class: "btn btn-primary", type: "button" }, icon("restart"), "Retry setup");
      retry.addEventListener("click", () => withBusy(retry, async () => {
        try {
          const chosen = picker && picker.hasChoice ? picker.value() : null;
          const a = session.agent;
          if (chosen && a && (chosen.model !== a.model || chosen.provider !== a.provider)) {
            const patched = await api("PATCH", agentUrl(session.agentId), { provider: chosen.provider, model: chosen.model });
            session.setAgent(patched.agent);
          }
          const res = await api("POST", agentUrl(session.agentId, "/setup/retry"));
          session.setAgent(res.agent);
          reset();
          setRow("files", "active", "Starting again…");
        } catch (err) {
          showError(err);
        }
      }));
      setChildren(errorBox, 
        h("div", { class: "notice notice-error", role: "alert" }, icon("alert"),
          h("div", null,
            h("strong", { text: stepLabel ? `Setup stopped at: ${stepLabel}` : "Setup failed" }),
            h("div", { text: message || "Something went wrong." }))),
        pickerHost,
        h("div", { class: "form-actions" }, needsSettings(null, message) ? settingsButton(null, true) : null, retry));
      errorBox.hidden = false;
    }

    function apply(ev) {
      if (ev.kind !== "agent_status") return;
      const d = ev.data || {};
      if (d.state === "creating") {
        if (d.step === "files") reset(); // a new attempt (first run or Retry) starts here
        errorBox.hidden = true;
        showingError = false;
        markUpTo(d.step, "active", d.message);
      } else if (d.state === "ready") {
        for (const [s] of SETUP_STEPS) {
          const st = rows.get(s).li.dataset.state;
          setRow(s, st === "pending" ? "skipped" : "done");
        }
        errorBox.hidden = true;
        showingError = false;
        readyBox.hidden = false;
      } else if (d.state === "error") {
        markUpTo(d.step, "error", d.message);
        showFailure(d.step, d.message);
      }
    }

    for (const ev of session.statusEvents) apply(ev);
    // The agent row is the authority: setup can be marked failed without an event (for
    // example when the platform restarted mid-setup), and old events may have scrolled
    // out of the history window.
    const agent = session.agent || {};
    if (agent.setup_state === "error" && !showingError) {
      const detail = String(agent.setup_detail || "");
      const sep = detail.indexOf(": ");
      const step = sep > 0 ? detail.slice(0, sep) : null;
      showFailure(step, sep > 0 ? detail.slice(sep + 2) : detail);
    } else if (agent.setup_state === "ready" && readyBox.hidden) {
      apply({ kind: "agent_status", data: { state: "ready", step: "ready" } });
    }
    onUnmount(session.on(apply));
  }

  // ======================================================================
  // 6. Agent session: one EventSource per open agent
  // ======================================================================

  /**
   * Everything the agent's pages share while they are open: the agent row, the chat timeline,
   * busy state and the event stream. Created when an agent page opens; closed when the user
   * navigates to a different agent or away from agent pages.
   */
  class AgentSession {
    constructor(agentId) {
      this.agentId = agentId;
      this.agent = null;
      this.lastSeq = 0;           // highest persisted seq seen; resumes the stream after reconnects
      this.closed = false;
      this.failed = false;
      this.loadingHistory = false;
      this.source = null;
      this.connState = "connecting";
      this.retryDelay = 2000;
      this.reconnectTimer = null;
      this.refreshTimer = null;
      this.listeners = new Set();       // fn(event) for every event
      this.changeListeners = new Set(); // fn() when agent row or busy state changes
      this.statusEvents = [];           // agent_status events, replayed by the setup screen
      this.partials = new Map();        // turn id -> Map(item id -> partial message text)
      this.busy = false;
      this.activeTurnId = null;
      this.stopping = false;
      this.busyChangedAt = 0;
      // Chat messages waiting to run, next first: [{turn_id, prompt, priority, queued_at}].
      // Built from GET /history's "queue", then kept current by turn_queued (adds) and
      // turn_started / turn_cancelled (remove). `leftQueue` remembers turns that already
      // started or were cancelled, so a late POST answer or replayed event cannot re-add them.
      this.queue = [];
      this.leftQueue = new Set();
      this.draft = "";
      this.expandedJobs = new Set();
      this.timeline = new Timeline({
        agentId,
        mode: "chat",
        emptyText: "No messages yet. Ask the agent to do something in its workspace.",
        actions: {
          newConversation: (button) => newConversation(this, button),
          switchModel: () => openSettings(this, { focusModel: true }),
        },
      });
      this.ready = this.open();
      // Views await `ready` and report errors themselves; this only marks the session as
      // failed so that the next visit builds a fresh one.
      this.ready.catch((err) => {
        this.failed = true;
        console.warn(`Could not open agent ${agentId}`, err);
      });
    }

    async open() {
      const [agentRes, history] = await Promise.all([
        api("GET", agentUrl(this.agentId)),
        api("GET", withQuery(agentUrl(this.agentId, "/history"), { limit: HISTORY_LIMIT })),
      ]);
      if (this.closed) return;
      this.agent = agentRes.agent;
      const events = (history && history.events) || [];
      if (events.length >= HISTORY_LIMIT) this.timeline.addNote("Older messages are not shown.");
      this.loadingHistory = true;
      try {
        for (const ev of events) this.dispatch(ev);
      } finally {
        this.loadingHistory = false;
      }
      this.lastSeq = Math.max(this.lastSeq, Number(history && history.last_seq) || 0);
      // The server's list is the authority: the replayed turn_queued events may have
      // scrolled out of the history window.
      if (history && Array.isArray(history.queue)) {
        this.queue = [];
        for (const q of history.queue) this.enqueue(q);
      }
      const active = history && history.active;
      if (active && active.turn_id) {
        this.partials.set(active.turn_id, new Map(Object.entries(active.items || {})));
        this.timeline.applySnapshot(active.turn_id, active.items || {});
      }
      // The agent row is the authority on the current state. Replayed events can leave a turn
      // looking unfinished (for example one cut off by a platform restart) or setup looking
      // in progress after the backend already marked it failed.
      this.agent = agentRes.agent;
      this.setBusy(!!this.agent.chat_busy, this.agent.active_turn_id || (active && active.turn_id) || null);
      if (!this.busy) this.timeline.finishDangling();
      this.connect();
    }

    connect() {
      if (this.closed) return;
      const source = new EventSource(withQuery(agentUrl(this.agentId, "/events"), { after: this.lastSeq }));
      this.source = source;
      source.onopen = () => {
        this.retryDelay = 2000;
        this.setConn("live");
        this.reconcile();
      };
      source.onmessage = (msg) => {
        let ev;
        try {
          ev = JSON.parse(msg.data);
        } catch (err) {
          console.warn("Ignoring an event that is not valid JSON", err);
          return;
        }
        this.dispatch(ev);
      };
      source.onerror = () => {
        if (this.closed || this.source !== source) return;
        if (source.readyState === EventSource.CLOSED) {
          // The browser gives up after a non-200 answer (signed out, agent deleted, server
          // restarting). Retry ourselves from the last seq we saw, backing off.
          source.close();
          this.setConn("offline");
          this.scheduleReconnect();
        } else {
          // The browser reconnects by itself and sends Last-Event-ID.
          this.setConn("reconnecting");
        }
      };
    }

    scheduleReconnect() {
      clearTimeout(this.reconnectTimer);
      const delay = this.retryDelay;
      this.retryDelay = Math.min(this.retryDelay * 2, 30000);
      this.reconnectTimer = setTimeout(async () => {
        if (this.closed) return;
        try {
          await this.refreshAgent();
        } catch (err) {
          if (err.status === 401 || this.closed) return; // signed out: the login redirect closed us
          if (err.status === 404) { this.handleGone(); return; }
          console.warn("Agent check before reconnecting failed; retrying", err);
          this.scheduleReconnect();
          return;
        }
        this.connect();
      }, delay);
    }

    /** After (re)connecting, make busy state match the server in case a turn ended while we were away. */
    async reconcile() {
      const askedAt = Date.now();
      let res;
      try {
        res = await api("GET", agentUrl(this.agentId));
      } catch (err) {
        if (err.status === 404) this.handleGone();
        else if (err.status !== 401) console.warn("Could not refresh the agent after reconnecting", err);
        return;
      }
      if (this.closed) return;
      this.setAgent(res.agent);
      // Ignore the answer if a turn started or ended (from our own events) after we asked.
      if (this.busyChangedAt <= askedAt && this.busy && !res.agent.chat_busy) {
        this.setBusy(false, null);
        this.timeline.finishDangling();
      }
    }

    handleGone() {
      if (this.closed) return;
      const wasOpen = state.session === this;
      closeSession();
      if (wasOpen) {
        toast("This agent no longer exists.", "info");
        go("#/agents");
      }
    }

    dispatch(ev) {
      if (this.closed || !ev || typeof ev !== "object") return;
      if (ev.seq !== null && ev.seq !== undefined) {
        if (ev.seq <= this.lastSeq) return; // already seen (history and stream overlap)
        this.lastSeq = ev.seq;
      }
      this.track(ev);
      this.timeline.apply(ev);
      for (const fn of Array.from(this.listeners)) {
        try { fn(ev); } catch (err) { console.error("An event handler failed", err); }
      }
    }

    /** Update shared state (busy, setup, container, partial text) from one event. */
    track(ev) {
      const d = ev.data || {};
      switch (ev.kind) {
        case "agent_status":
          this.statusEvents.push(ev);
          if (this.statusEvents.length > 200) this.statusEvents.shift();
          if (this.agent) {
            if (["creating", "ready", "error"].includes(d.state)) {
              this.agent.setup_state = d.state;
              if (d.state === "error") this.agent.setup_detail = `${d.step}: ${d.message}`;
            } else if (["starting", "running", "stopped", "restarting"].includes(d.state)) {
              this.agent.container = d.state;
            }
          }
          this.notify();
          this.scheduleRefresh();
          break;
        case "turn_queued":
          if (ev.channel === "chat") {
            this.enqueue({ turn_id: ev.turn_id, prompt: d.prompt, priority: d.priority, queued_at: ev.at });
            this.notify();
          }
          break;
        case "turn_cancelled":
          if (ev.channel === "chat") this.dequeue(ev.turn_id);
          break;
        case "turn_started":
          if (ev.channel === "chat") {
            this.dequeue(ev.turn_id);
            this.setBusy(true, ev.turn_id);
          }
          break;
        case "turn_completed":
          this.partials.delete(ev.turn_id);
          if (ev.channel === "chat" && (!this.activeTurnId || this.activeTurnId === ev.turn_id)) this.setBusy(false, null);
          this.scheduleRefresh();
          break;
        case "snapshot": {
          const turnId = d.turn_id || ev.turn_id;
          if (turnId) this.partials.set(turnId, new Map(Object.entries(d.items || {})));
          if (turnId && ev.channel === "chat") this.setBusy(true, turnId);
          break;
        }
        case "message_delta": {
          let partial = this.partials.get(ev.turn_id);
          if (!partial) { partial = new Map(); this.partials.set(ev.turn_id, partial); }
          partial.set(d.item_id, (partial.get(d.item_id) || "") + (d.delta || ""));
          break;
        }
        case "item_completed": {
          const partial = this.partials.get(ev.turn_id);
          if (partial) partial.delete(d.item_id);
          break;
        }
        default:
          break;
      }
    }

    /** Add a waiting message in run order: "next" (priority 1) ones before ordinary ones. */
    enqueue(item) {
      if (!item || !item.turn_id || this.leftQueue.has(item.turn_id)) return;
      if (this.queue.some((q) => q.turn_id === item.turn_id)) return;
      const entry = {
        turn_id: item.turn_id,
        prompt: String(item.prompt || ""),
        priority: Number(item.priority) === 1 ? 1 : 0,
        queued_at: item.queued_at || null,
      };
      const at = entry.priority === 1 ? this.queue.findIndex((q) => q.priority === 0) : -1;
      if (at < 0) this.queue.push(entry);
      else this.queue.splice(at, 0, entry);
    }

    /** A queued message started or was cancelled: it leaves the queue for good. */
    dequeue(turnId) {
      if (!turnId) return;
      this.leftQueue.add(turnId);
      const before = this.queue.length;
      this.queue = this.queue.filter((q) => q.turn_id !== turnId);
      if (this.queue.length !== before) this.notify();
    }

    setBusy(busy, turnId) {
      this.busy = busy;
      this.activeTurnId = busy ? turnId : null;
      if (!busy) this.stopping = false;
      this.busyChangedAt = Date.now();
      this.timeline.setWorking(busy, this.stopping ? "Stopping…" : "Working…");
      this.notify();
    }

    setStopping(stopping) {
      this.stopping = stopping;
      this.timeline.setWorking(this.busy, stopping ? "Stopping…" : "Working…");
      this.notify();
    }

    setAgent(agent) {
      if (!agent || this.closed) return;
      this.agent = agent;
      this.notify();
    }

    setConn(connState) {
      this.connState = connState;
      this.notify();
    }

    scheduleRefresh() {
      if (this.closed || this.loadingHistory) return;
      clearTimeout(this.refreshTimer);
      this.refreshTimer = setTimeout(() => {
        this.refreshAgent().catch((err) => {
          if (err.status === 404) this.handleGone();
          else if (err.status !== 401) console.warn("Could not refresh the agent", err);
        });
      }, 400);
    }

    async refreshAgent() {
      const res = await api("GET", agentUrl(this.agentId));
      this.setAgent(res.agent);
      return res.agent;
    }

    notify() {
      for (const fn of Array.from(this.changeListeners)) {
        try { fn(); } catch (err) { console.error("A change handler failed", err); }
      }
    }

    on(fn) {
      this.listeners.add(fn);
      return () => this.listeners.delete(fn);
    }

    onChange(fn) {
      this.changeListeners.add(fn);
      return () => this.changeListeners.delete(fn);
    }

    close() {
      this.closed = true;
      if (this.source) this.source.close();
      this.source = null;
      clearTimeout(this.reconnectTimer);
      clearTimeout(this.refreshTimer);
      this.listeners.clear();
      this.changeListeners.clear();
    }
  }

  // ======================================================================
  // 7. Timeline renderer (chat and job-run transcripts)
  // ======================================================================

  // The server's error_message for a turn stopped by "Interrupt & send" (codex_bridge.INTERRUPTED_BY_NEWER).
  const INTERRUPTED_BY_NEWER = "Interrupted by a newer message.";

  // Short headings for the red failed-turn card; the server's error_message is the body.
  const ERROR_TITLES = {
    unauthorized: "Credentials rejected",
    no_api_key: "No API key",
    rate_limited: "Rate limited",
    model_unavailable: "Model unavailable",
    context_full: "Conversation full",
    runtime_crashed: "Runtime crashed",
    container_failed: "Container failed",
    timeout: "Timed out",
    platform_restart: "Platform restarted",
    other: "Turn failed",
  };

  // ---- Syntax colouring for fenced code -----------------------------------
  // A small tokenizer, not a parser: comments, strings, numbers, keywords,
  // type names and function calls get a class, everything else stays plain.
  // Keywords for the common languages are merged into one set; a word that
  // is a keyword in one language and a name in another rarely matters for
  // colouring. Built from text nodes only, like the Markdown renderer.

  // Control flow and imports get their own colour, as in most editor themes.
  const HL_CONTROL = new Set((
    "break case catch continue default do else for goto if return switch throw try while yield await " +
    "import export from elif except finally raise pass with match loop defer go"
  ).split(" "));
  const HL_KEYWORDS = new Set((
    "auto class const constexpr delete enum explicit extends extern final friend implements inline " +
    "interface namespace new noexcept operator override private protected public sizeof static struct " +
    "template this throws typedef typename union using virtual volatile async function let var " +
    "instanceof typeof def global lambda nonlocal as is not and or in del assert fn mut impl pub use " +
    "mod trait crate dyn func chan package"
  ).split(" "));
  const HL_LITERALS = new Set("true false null nullptr NULL None True False undefined nil self".split(" "));
  const HL_TYPES = new Set((
    "int long short char float double bool boolean void unsigned signed size_t string str byte " +
    "vector map set unordered_map unordered_set priority_queue queue stack deque pair list dict tuple " +
    "array optional shared_ptr unique_ptr i8 i16 i32 i64 u8 u16 u32 u64 f32 f64 usize isize"
  ).split(" "));
  // Languages whose comments start with "#". The rest use // and /* */.
  const HL_HASH = new Set("python py sh bash shell zsh ruby rb yaml yml toml r perl dockerfile makefile ini conf".split(" "));
  // Fences that are output or prose, not code: shown uncoloured.
  const HL_PLAIN = new Set("text txt plain plaintext output console log logs".split(" "));
  const HL_STRING = /"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/.source;
  const HL_NUMBER = /\b(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)[a-zA-Z]*\b/.source;

  function hlRegex(hashComments) {
    return new RegExp([
      hashComments ? /(#[^\n]*)/.source : /(\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$))/.source,
      `(${HL_STRING})`,
      `(${HL_NUMBER})`,
      hashComments ? "((?!))" : /(^[ \t]*#[ \t]*[a-z]+\b)/.source, // C preprocessor: #include
      /([A-Za-z_$][\w$]*)/.source,
    ].join("|"), "gm");
  }

  /** A <code> element for `text`, coloured when `lang` names a programming language. */
  function highlightCode(text, lang) {
    const code = h("code");
    if (!lang || HL_PLAIN.has(lang)) { code.textContent = text; return code; }
    const re = hlRegex(HL_HASH.has(lang));
    const span = (cls, str) => code.appendChild(h("span", { class: `tok-${cls}`, text: str }));
    let last = 0;
    for (let m; (m = re.exec(text)); ) {
      if (m.index > last) code.appendChild(document.createTextNode(text.slice(last, m.index)));
      last = re.lastIndex;
      if (m[1]) span("com", m[1]);
      else if (m[2]) span("str", m[2]);
      else if (m[3]) span("num", m[3]);
      else if (m[4]) span("ctl", m[4]);
      else {
        const word = m[5];
        if (HL_CONTROL.has(word)) span("ctl", word);
        else if (HL_KEYWORDS.has(word)) span("kw", word);
        else if (HL_LITERALS.has(word)) span("lit", word);
        else if (HL_TYPES.has(word) || /_t$/.test(word) || (/^[A-Z]/.test(word) && /[a-z]/.test(word))) span("type", word);
        else if (/^\s*\(/.test(text.slice(last, last + 40))) span("fn", word);
        else span("var", word);
      }
    }
    if (last < text.length) code.appendChild(document.createTextNode(text.slice(last)));
    return code;
  }

  // ---- Markdown for agent messages --------------------------------------
  // A deliberately small Markdown subset (what models actually write):
  // fenced code, headings, bullet and numbered lists, quotes, rules,
  // paragraphs, and inline code, **bold**, *italic*, ~~strike~~, links and
  // bare URLs. Every node is built with createElement/textContent, never
  // innerHTML, so text from the model cannot inject markup or script.

  /** Render agent-message Markdown into `el` (replacing its children). */
  function renderRichText(el, text) {
    el.replaceChildren();
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    let para = []; // lines of the paragraph being collected

    const flushPara = () => {
      if (!para.length) return;
      const p = h("p");
      para.forEach((line, n) => {
        if (n) p.appendChild(h("br")); // models use single newlines on purpose
        appendInlineMd(p, line);
      });
      el.appendChild(p);
      para = [];
    };

    while (i < lines.length) {
      const line = lines[i];
      let m;
      if ((m = line.match(/^\s*```\s*([^`]*)$/))) { // fenced code block
        flushPara();
        const body = [];
        i += 1;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
        i += 1; // skip the closing fence (or run off the end while streaming)
        const lang = (m[1].trim().split(/\s+/)[0] || "").toLowerCase();
        const pre = h("pre", { class: "code-block" }, highlightCode(body.join("\n"), lang));
        if (lang) pre.dataset.lang = lang;
        el.appendChild(pre);
        continue;
      }
      if (!line.trim()) { flushPara(); i += 1; continue; }
      if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))) { // heading
        flushPara();
        const level = Math.min(m[1].length + 2, 6); // keep headings small inside a bubble
        const hd = h(`h${level}`, { class: "md-heading" });
        appendInlineMd(hd, m[2]);
        el.appendChild(hd);
        i += 1;
        continue;
      }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { flushPara(); el.appendChild(h("hr")); i += 1; continue; }
      if (/^\s{0,3}>/.test(line)) { // blockquote
        flushPara();
        const quoted = [];
        while (i < lines.length && /^\s{0,3}>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s{0,3}>\s?/, ""));
        const bq = h("blockquote");
        renderRichText(bq, quoted.join("\n"));
        el.appendChild(bq);
        continue;
      }
      const bullet = /^(\s*)[-*+]\s+(.*)$/;
      const numbered = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
      if (bullet.test(line) || numbered.test(line)) { // list (one level, plus continuation lines)
        flushPara();
        const ordered = numbered.test(line);
        const list = h(ordered ? "ol" : "ul");
        if (ordered) list.start = Number(line.match(numbered)[2]);
        let item = null;
        while (i < lines.length) {
          const cur = lines[i];
          const b = cur.match(bullet);
          const n = cur.match(numbered);
          if (ordered ? n : b) {
            item = h("li");
            appendInlineMd(item, ordered ? n[3] : b[2]);
            list.appendChild(item);
            i += 1;
          } else if (item && cur.trim() && /^\s+/.test(cur)) { // indented continuation
            item.appendChild(h("br"));
            appendInlineMd(item, cur.trim().replace(/^[-*+]\s+/, "• "));
            i += 1;
          } else {
            break;
          }
        }
        el.appendChild(list);
        continue;
      }
      para.push(line);
      i += 1;
    }
    flushPara();
  }

  // Inline tokens, tried left to right; the earliest match wins.
  const INLINE_RE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*|__[^_\n]+?__)|(~~[^~\n]+?~~)|(\[[^\]\n]+\]\((?:[^()\s]|\([^()\s]*\))+\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"\]])|(\*[^*\s][^*\n]*?\*|\b_[^_\s][^_\n]*?_\b)/;

  /** Append `text` to `parent`, turning inline Markdown into elements. */
  function appendInlineMd(parent, text) {
    let rest = String(text);
    while (rest) {
      const m = rest.match(INLINE_RE);
      if (!m) { parent.appendChild(document.createTextNode(rest)); return; }
      if (m.index) parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
      const tok = m[0];
      if (m[1]) {
        parent.appendChild(h("code", { class: "inline-code", text: tok.slice(1, -1) }));
      } else if (m[2]) {
        const b = h("strong"); appendInlineMd(b, tok.slice(2, -2)); parent.appendChild(b);
      } else if (m[3]) {
        const del = h("del"); appendInlineMd(del, tok.slice(2, -2)); parent.appendChild(del);
      } else if (m[4]) {
        const lm = tok.match(/^\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)$/); // allows one level of (parens) in URLs
        parent.appendChild(mdLink(lm[1], lm[2]));
      } else if (m[5]) {
        parent.appendChild(mdLink(tok, tok));
      } else {
        const em = h("em"); appendInlineMd(em, tok.slice(1, -1)); parent.appendChild(em);
      }
      rest = rest.slice(m.index + tok.length);
    }
  }

  /** A span of inline Markdown (used for short labels such as schedules). */
  function richInline(text) {
    const span = h("span", { class: "rich-text" });
    appendInlineMd(span, text);
    return span;
  }

  /** A safe link: web/mail links open in a new tab; /workspace/... opens the Files tab. */
  function mdLink(label, href) {
    let url = null;
    const agentMatch = location.hash.match(/^#\/agent\/([0-9a-f]{32})/);
    const ws = href.match(/^(?:\/workspace\/|\.\/|)([^:]*)$/);
    if (/^(https?:|mailto:)/i.test(href)) {
      url = href;
    } else if (agentMatch && ws && (href.startsWith("/workspace/") || !href.includes("/") || href.startsWith("./"))) {
      // A file the agent mentions: open its folder in the Files tab.
      const path = decodeURIComponent(ws[1]).replace(/^\/+/, "");
      const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      url = `#/agent/${agentMatch[1]}/files?path=${encodeURIComponent(dir)}`;
    }
    if (!url) return document.createTextNode(label); // javascript:, data:, etc. stay plain text
    const a = h("a", { href: url, class: "md-link" });
    appendInlineMd(a, label);
    if (!url.startsWith("#")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
    return a;
  }

  // ---- item entries: one per Codex item, updated in place as events arrive --

  class MessageEntry {
    constructor(tl) {
      this.tl = tl;
      this.text = "";
      this.done = false;
      this.pending = false;
      this.body = h("div", { class: "bubble rich" });
      this.el = h("div", { class: "msg msg-agent streaming" }, this.body);
    }
    update(d, completed) {
      if (completed) {
        if (typeof d.text === "string" && d.text) this.text = d.text;
        this.finish();
      } else if (typeof d.text === "string" && d.text.length > this.text.length) {
        this.text = d.text;
      }
      this.renderNow();
    }
    appendDelta(delta) {
      this.text += delta;
      this.scheduleRender();
    }
    setText(text) {
      this.text = text;
      this.scheduleRender();
    }
    // Deltas arrive word by word; render at most once per frame.
    scheduleRender() {
      if (this.pending) return;
      this.pending = true;
      requestAnimationFrame(() => {
        this.pending = false;
        const stick = this.tl.isNearBottom();
        this.renderNow();
        if (stick) this.tl.scrollToBottom();
      });
    }
    renderNow() {
      renderRichText(this.body, this.text);
    }
    finish() {
      this.done = true;
      this.el.classList.remove("streaming");
    }
  }

  // Tool activity (commands, file changes, tool calls, searches, reasoning) is kept out of the
  // way: every such item of a turn goes into one collapsed "Worked · N steps" row (see
  // ActivityGroup). Inside it, rows are neutral even when a command exits non-zero, because a
  // failing command is a normal part of an agent's work, not something the user must act on.
  // Each entry below has update(d, completed), finish(reason), `done`, and stepLabel() (a
  // one-line description for the collapsed row). `visible` is false for rows with nothing to show.

  class ReasoningEntry {
    constructor() {
      this.done = false;
      this.visible = true;
      this.text = "";
      this.el = h("div", { class: "thinking" }, spinner(), h("span", { text: "Thinking…" }));
    }
    update(d, completed) {
      if (!completed) return;
      this.done = true;
      const text = typeof d.text === "string" ? d.text.trim() : "";
      this.text = text;
      if (!text) {
        // Nothing to show once the thought is over; keep the timeline quiet.
        this.el.hidden = true;
        this.visible = false;
        return;
      }
      const details = h("details", { class: "thinking thinking-done" },
        h("summary", null, icon("note"), "Thought"),
        h("p", { class: "thinking-text", text }));
      this.el.replaceWith(details);
      this.el = details;
    }
    stepLabel() {
      return this.done ? "Thought" : "Thinking";
    }
    finish() {
      if (this.done) return;
      this.done = true;
      this.el.hidden = true;
      this.visible = false;
    }
  }

  class CommandEntry {
    constructor(tl) {
      this.tl = tl;
      this.output = "";
      this.cmd = "";
      this.done = false;
      this.visible = true;
      this.pending = false;
      this.cmdEl = h("code", { class: "tool-cmd" });
      this.badges = h("span", { class: "tool-badges" });
      this.outEl = h("pre", { class: "tool-output", hidden: true });
      const summary = h("summary", { class: "tool-summary" },
        icon("terminal", "tool-kind"), this.cmdEl, this.badges);
      // Output stays collapsed (also while running and when the command failed); the user
      // opens a row to read it.
      this.el = h("details", { class: "tool tool-command is-running" }, summary, this.outEl);
    }
    update(d, completed) {
      const cmd = Array.isArray(d.command) ? d.command.join(" ") : String(d.command || "");
      if (cmd) {
        this.cmd = cmd;
        this.cmdEl.textContent = cmd;
        this.cmdEl.title = cmd;
      }
      // The completed item carries the authoritative output tail; keep live output otherwise.
      if (typeof d.output === "string" && d.output && (completed || d.output.length > this.output.length)) this.output = d.output;
      const status = d.status || (completed ? "completed" : "inProgress");
      this.done = completed || status !== "inProgress";
      const exit = d.exit_code;
      const hasExit = exit !== null && exit !== undefined;
      const parts = [];
      if (!this.done) parts.push(spinner());
      // The exit code is shown as plain information, never coloured as an error.
      if (hasExit) parts.push(badge(`exit ${exit}`, "muted"));
      else if (status === "failed" || status === "declined") parts.push(badge(status, "muted"));
      if (typeof d.duration_ms === "number") parts.push(h("span", { class: "tool-dur", text: fmtDuration(d.duration_ms) }));
      setChildren(this.badges, ...parts);
      this.el.classList.toggle("is-running", !this.done);
      this.renderOutput();
    }
    appendOutput(delta) {
      this.output += delta;
      if (this.output.length > COMMAND_OUTPUT_KEEP) this.output = this.output.slice(-COMMAND_OUTPUT_KEEP);
      if (this.pending) return;
      this.pending = true;
      requestAnimationFrame(() => {
        this.pending = false;
        this.renderOutput();
      });
    }
    renderOutput() {
      const text = this.output || (this.done ? "(no output)" : "");
      this.outEl.textContent = text;
      this.outEl.hidden = !text;
      if (!this.done) this.outEl.scrollTop = this.outEl.scrollHeight;
    }
    stepLabel() {
      return this.cmd ? `Ran ${this.cmd}` : "Ran a command";
    }
    finish(reason) {
      if (this.done) return;
      this.done = true;
      this.el.classList.remove("is-running");
      setChildren(this.badges, badge(reason === "unfinished" ? "did not finish" : "stopped", "muted"));
      this.renderOutput();
    }
  }

  class FileChangeEntry {
    constructor(tl) {
      this.tl = tl;
      this.done = false;
      this.visible = true;
      this.count = 0;
      this.list = h("ul", { class: "file-changes" });
      this.badges = h("span", { class: "tool-badges" });
      this.el = h("div", { class: "tool tool-files" },
        h("div", { class: "tool-head" }, icon("file", "tool-kind"), h("span", { class: "tool-title", text: "Changed files" }), this.badges),
        this.list);
    }
    update(d, completed) {
      const status = d.status || (completed ? "completed" : "inProgress");
      this.done = completed || status !== "inProgress";
      const changes = Array.isArray(d.changes) ? d.changes : [];
      this.count = changes.length;
      setChildren(this.list, ...changes.map((c) => this.changeRow(c || {})));
      const parts = [];
      if (!this.done) parts.push(spinner());
      if (status === "failed" || status === "declined") parts.push(badge(status, "muted"));
      setChildren(this.badges, ...parts);
    }
    changeRow(change) {
      // `kind` may be a plain string or the SDK's {type: "add"|"delete"|"update"} object.
      const raw = change.kind && typeof change.kind === "object" ? change.kind.type : change.kind;
      const kind = { add: "added", delete: "deleted", update: "edited" }[raw] || raw || "changed";
      const tone = kind === "added" ? "ok" : kind === "edited" ? "info" : "muted";
      const rel = workspaceRelative(change.path);
      let target;
      if (rel === null) target = h("span", { class: "mono", text: String(change.path || "") });
      else if (kind === "deleted") target = h("a", { class: "mono", href: filesHash(this.tl.agentId, dirname(rel)), text: rel || "/" });
      else target = h("a", { class: "mono", href: filesHash(this.tl.agentId, dirname(rel), rel), text: rel || "/" });
      return h("li", null, badge(kind, tone), target);
    }
    stepLabel() {
      return this.count === 1 ? "Changed 1 file" : `Changed ${this.count || "some"} files`;
    }
    finish() {
      this.done = true;
      this.badges.replaceChildren();
    }
  }

  class McpEntry {
    constructor() {
      this.done = false;
      this.visible = true;
      this.title = h("span", { class: "tool-title" });
      this.badges = h("span", { class: "tool-badges" });
      this.preview = h("span", { class: "tool-preview" });
      this.body = h("div", { class: "tool-body" });
      this.el = h("details", { class: "tool tool-mcp" },
        h("summary", { class: "tool-summary" },
          icon("box", "tool-kind"),
          h("span", { class: "tool-summary-text" }, this.title, this.preview),
          this.badges),
        this.body);
    }
    update(d, completed) {
      const server = d.server || "platform";
      const tool = d.tool || "tool";
      this.title.textContent = server === "platform" ? `Used platform tool ${tool}` : `Used tool ${server} › ${tool}`;
      const status = d.status || (completed ? "completed" : "inProgress");
      this.done = completed || status !== "inProgress";
      const err = errorText(d.error);
      const failed = status === "failed" || !!err;
      const parts = [];
      if (!this.done) parts.push(spinner());
      if (failed) parts.push(badge("failed", "muted"));
      setChildren(this.badges, ...parts);
      const result = typeof d.result_text === "string" ? d.result_text : "";
      const first = (err || result).split("\n")[0];
      this.preview.textContent = first.length > 160 ? first.slice(0, 157) + "…" : first;
      setChildren(this.body,
        d.arguments ? h("div", { class: "tool-section" }, h("div", { class: "tool-section-label", text: "Arguments" }), h("pre", { class: "tool-output", text: prettyJson(d.arguments) })) : null,
        result ? h("div", { class: "tool-section" }, h("div", { class: "tool-section-label", text: "Result" }), h("pre", { class: "tool-output", text: result })) : null,
        err ? h("div", { class: "tool-section" }, h("div", { class: "tool-section-label", text: "Error" }), h("pre", { class: "tool-output", text: err })) : null);
    }
    stepLabel() {
      return this.title.textContent || "Used a tool";
    }
    finish() {
      this.done = true;
      this.badges.replaceChildren();
    }
  }

  class LineEntry {
    // A one-line step: web searches and item types the platform does not know.
    constructor(kind) {
      this.kind = kind;
      this.done = false;
      this.visible = true;
      this.label = h("span");
      this.badges = h("span", { class: "tool-badges" });
      this.el = h("div", { class: "tool-line" }, icon(kind === "web_search" ? "search" : "box", "tool-kind"), this.label, this.badges);
    }
    update(d, completed) {
      this.done = completed;
      if (this.kind === "web_search") this.label.textContent = d.query ? `Searched the web: ${d.query}` : "Searched the web";
      else this.label.textContent = `Agent step: ${d.raw_type || d.item_type || "unknown"}`;
      setChildren(this.badges, completed ? null : spinner());
    }
    stepLabel() {
      return this.label.textContent || "Worked";
    }
    finish() {
      this.done = true;
      this.badges.replaceChildren();
    }
  }

  function createEntry(tl, itemType) {
    switch (itemType) {
      case "agent_message": return new MessageEntry(tl);
      case "reasoning": return new ReasoningEntry(tl);
      case "command": return new CommandEntry(tl);
      case "file_change": return new FileChangeEntry(tl);
      case "mcp_tool_call": return new McpEntry(tl);
      case "web_search": return new LineEntry("web_search");
      case "user_message": return null; // the prompt is already shown from turn_started
      default: return new LineEntry("other");
    }
  }

  /**
   * One collapsed row holding all tool activity of a turn, placed where its first step
   * appeared: "Working · 2 steps · Ran ls" while the turn runs, "Worked · 4 steps" after.
   * Expanding it shows the individual rows. Failures are never counted or advertised here.
   */
  class ActivityGroup {
    constructor() {
      this.entries = [];
      this.running = true; // until the turn ends (between steps the turn is still working)
      this.chevron = icon("chevron", "activity-chevron");
      this.stateEl = h("span", { class: "activity-state" });
      this.labelEl = h("span", { class: "activity-label" });
      this.currentEl = h("span", { class: "activity-current" });
      this.body = h("div", { class: "activity-body" });
      this.el = h("details", { class: "activity" },
        h("summary", { class: "activity-summary" }, this.chevron, this.stateEl, this.labelEl, this.currentEl),
        this.body);
    }
    add(entry) {
      this.entries.push(entry);
      entry.group = this;
      this.body.appendChild(entry.el);
      this.refresh();
    }
    finish() {
      this.running = false;
      this.refresh();
    }
    refresh() {
      const shown = this.entries.filter((e) => e.visible);
      const n = shown.length;
      const steps = n === 1 ? "1 step" : `${n} steps`;
      // A turn whose only step was an empty thought leaves nothing worth a row.
      this.el.hidden = !this.running && n === 0;
      this.el.classList.toggle("is-running", this.running);
      setChildren(this.stateEl, this.running ? spinner() : icon("check", "activity-done"));
      if (this.running) {
        this.labelEl.textContent = n ? `Working · ${steps}` : "Working…";
        const live = [...this.entries].reverse().find((e) => !e.done) || shown[shown.length - 1];
        const text = live ? live.stepLabel() : "";
        this.currentEl.textContent = text;
        this.currentEl.title = text;
      } else {
        this.labelEl.textContent = `Worked · ${steps}`;
        this.currentEl.textContent = "";
        this.currentEl.removeAttribute("title");
      }
    }
  }

  /**
   * Renders events into a scrolling list. In "chat" mode it shows the agent's chat channel plus
   * agent-level cards; in "run" mode it shows exactly one job run's turn.
   */
  class Timeline {
    constructor(opts) {
      this.agentId = opts.agentId;
      this.mode = opts.mode;
      this.turnId = opts.turnId || null;
      this.actions = opts.actions || {};
      this.root = h("div", { class: `timeline timeline-${opts.mode}`, role: "log" });
      this.emptyEl = h("div", { class: "timeline-empty", text: opts.emptyText || "Nothing here yet." });
      this.workingLabel = h("span", { class: "working-label", text: "Working…" });
      this.workingEl = h("div", { class: "working" }, spinner(), this.workingLabel);
      this.working = false;
      this.items = new Map();       // "<turn id>:<item id>" -> entry
      this.turns = new Map();       // turn id -> per-turn state (see turn())
      this.jobRunCards = new Map(); // run id -> "started" card, replaced when the run finishes
      this.maxSeq = 0;
      this.root.appendChild(this.emptyEl);
    }

    accepts(ev) {
      const turnId = ev.kind === "snapshot" ? (ev.data && ev.data.turn_id) || ev.turn_id : ev.turn_id;
      if (this.mode === "run") return !!this.turnId && turnId === this.turnId;
      if (ev.kind === "snapshot") return ev.channel === "chat" || this.turns.has(turnId);
      if (ev.channel === "chat") return true;
      if (ev.channel === "agent") return ["agent_status", "job_run_started", "job_run_finished", "notice"].includes(ev.kind);
      return false; // job-run turns have their own transcript
    }

    apply(ev) {
      if (!ev || !this.accepts(ev)) return;
      if (ev.seq !== null && ev.seq !== undefined) {
        if (ev.seq <= this.maxSeq) return;
        this.maxSeq = ev.seq;
      }
      const stick = this.isNearBottom();
      const d = ev.data || {};
      switch (ev.kind) {
        case "turn_started": this.renderTurnStarted(ev.turn_id, d, ev.at); break;
        case "item_started": this.renderItem(ev, false); break;
        case "item_completed": this.renderItem(ev, true); break;
        case "message_delta": this.appendDelta(ev.turn_id, d); break;
        case "command_output": this.appendCommandOutput(ev.turn_id, d); break;
        case "notice": this.renderNotice(ev.turn_id, d); break;
        case "turn_completed": this.renderTurnCompleted(ev.turn_id, d); break;
        case "agent_status": this.renderAgentStatus(d); break;
        case "job_run_started":
        case "job_run_finished": this.renderJobRunCard(ev.kind, d, ev.at); break;
        case "snapshot": this.applySnapshot(d.turn_id || ev.turn_id, d.items); break;
        default: break; // for example jobs_changed: nothing to draw
      }
      this.updateEmpty();
      if (stick) this.scrollToBottom();
    }

    // ---- layout helpers ----

    add(el) {
      // New content goes above the "Working…" indicator, which always stays last.
      this.root.insertBefore(el, this.workingEl.parentNode === this.root ? this.workingEl : null);
    }

    addNote(text) {
      this.root.insertBefore(h("div", { class: "divider divider-muted" }, h("span", { text })), this.root.firstChild);
    }

    updateEmpty() {
      let count = this.root.childElementCount;
      if (this.emptyEl.parentNode === this.root) count -= 1;
      if (this.workingEl.parentNode === this.root) count -= 1;
      if (count > 0 || this.working) this.emptyEl.remove();
      else if (this.emptyEl.parentNode !== this.root) this.root.prepend(this.emptyEl);
    }

    setWorking(on, label) {
      this.working = on;
      this.workingLabel.textContent = label || "Working…";
      if (on && this.workingEl.parentNode !== this.root) {
        const stick = this.isNearBottom();
        this.root.appendChild(this.workingEl);
        if (stick) this.scrollToBottom();
      } else if (!on) {
        this.workingEl.remove();
      }
      this.updateEmpty();
    }

    isNearBottom() {
      const r = this.root;
      return r.scrollHeight - r.scrollTop - r.clientHeight < 120;
    }

    scrollToBottom() {
      this.root.scrollTop = this.root.scrollHeight;
    }

    turn(turnId) {
      const key = turnId || "";
      let t = this.turns.get(key);
      if (!t) {
        t = { started: false, items: new Set(), hasMessage: false, failedMessage: null, errorNotices: [], activity: null };
        this.turns.set(key, t);
      }
      return t;
    }

    // ---- event renderers ----

    /** Show the user's message right away after a send; turn_started for it is then skipped. */
    addUserMessage(turnId, text) {
      const t = this.turn(turnId);
      if (t.started) return;
      t.started = true;
      this.add(this.userBubble(text, new Date().toISOString()));
      this.updateEmpty();
      this.scrollToBottom();
    }

    userBubble(text, at) {
      return h("div", { class: "msg msg-user" },
        h("div", { class: "bubble", text }),
        h("div", { class: "msg-meta", title: fmtDateTime(at), text: fmtClock(at) }));
    }

    renderTurnStarted(turnId, d, at) {
      // Chat turns run one at a time, so an earlier turn whose end we never saw (for
      // example cut off by a restart) is over: stop its "Working" row.
      for (const [id, other] of this.turns) {
        if (id !== (turnId || "") && other.activity && other.activity.running) other.activity.finish();
      }
      const t = this.turn(turnId);
      if (t.started) return;
      t.started = true;
      if (this.mode === "run" || d.source === "job") {
        this.add(h("div", { class: "msg msg-prompt" },
          h("div", { class: "msg-label" }, icon("clock"), d.job_name ? `Scheduled prompt · ${d.job_name}` : "Scheduled prompt"),
          h("div", { class: "bubble", text: d.prompt || "" })));
      } else {
        this.add(this.userBubble(d.prompt || "", at));
      }
    }

    renderItem(ev, completed) {
      const d = ev.data || {};
      const key = `${ev.turn_id}:${d.item_id}`;
      let entry = this.items.get(key);
      if (!entry) {
        entry = createEntry(this, d.item_type);
        if (!entry) return;
        this.place(ev.turn_id, key, entry);
      }
      entry.update(d, completed);
      if (entry.group) entry.group.refresh();
    }

    /** Put a new entry on the timeline: messages as bubbles, everything else in the turn's activity row. */
    place(turnId, key, entry) {
      this.items.set(key, entry);
      const t = this.turn(turnId);
      t.items.add(key);
      if (entry instanceof MessageEntry) {
        t.hasMessage = true;
        this.add(entry.el);
        return;
      }
      if (!t.activity) {
        t.activity = new ActivityGroup();
        this.add(t.activity.el);
      }
      t.activity.add(entry);
    }

    messageEntry(turnId, itemId) {
      const key = `${turnId}:${itemId}`;
      let entry = this.items.get(key);
      if (!entry) {
        entry = new MessageEntry(this);
        this.place(turnId, key, entry);
      }
      return entry instanceof MessageEntry ? entry : null;
    }

    appendDelta(turnId, d) {
      const entry = this.messageEntry(turnId, d.item_id);
      if (entry && !entry.done) entry.appendDelta(String(d.delta || ""));
    }

    appendCommandOutput(turnId, d) {
      const key = `${turnId}:${d.item_id}`;
      let entry = this.items.get(key);
      if (!entry) {
        // Output arrived before (or without) its item_started: draw the row anyway.
        entry = new CommandEntry(this);
        this.place(turnId, key, entry);
        entry.update({ status: "inProgress" }, false);
        entry.group.refresh();
      }
      if (entry instanceof CommandEntry) entry.appendOutput(String(d.delta || ""));
    }

    applySnapshot(turnId, items) {
      if (!turnId || !items) return;
      for (const [itemId, text] of Object.entries(items)) {
        const entry = this.messageEntry(turnId, itemId);
        // The snapshot is the full text so far, so it replaces rather than appends.
        if (entry && !entry.done) entry.setText(String(text || ""));
      }
      this.updateEmpty();
    }

    renderNotice(turnId, d) {
      const level = ["info", "warning", "error"].includes(d.level) ? d.level : "info";
      const message = String(d.message || "");
      const t = turnId ? this.turn(turnId) : null;
      // A failed turn also emits an error notice with the same text; the red card says it once.
      if (level === "error" && t && t.failedMessage === message) return;
      // Only errors get a banner; info and warnings (for example "Reconnecting to the model…")
      // are a small muted line, since the user has nothing to do about them.
      let el;
      if (level === "error") el = h("div", { class: "notice notice-error", role: "alert" }, icon("alert"), h("span", { text: message }));
      else el = h("div", { class: `quiet-note quiet-note-${level}` }, h("span", { text: message }));
      if (level === "error" && t) t.errorNotices.push({ el, message });
      this.add(el);
    }

    renderTurnCompleted(turnId, d) {
      const t = this.turn(turnId);
      this.finishTurn(turnId, d.status);
      if (d.status === "failed") {
        const message = d.error_message || "The agent could not finish this message.";
        t.failedMessage = message;
        for (const n of t.errorNotices) if (n.message === message) n.el.remove();
        this.add(this.failedCard(d.error_kind, message));
      } else if (d.status === "interrupted") {
        // Interrupt-and-send ends the running turn with this exact message; say so instead of "Stopped.".
        const byNewer = d.error_message === INTERRUPTED_BY_NEWER;
        const message = byNewer ? INTERRUPTED_BY_NEWER
          : d.error_kind === "interrupted" || !d.error_message ? "Stopped." : d.error_message;
        this.add(h("div", { class: "divider divider-muted" }, h("span", { text: message })));
      } else if (d.status === "completed" && !t.hasMessage && d.final_response) {
        // The reply's message item is missing (for example cut off by the history window).
        const entry = new MessageEntry(this);
        entry.text = String(d.final_response);
        entry.finish();
        entry.renderNow();
        t.hasMessage = true;
        this.add(entry.el);
      }
    }

    failedCard(kind, message) {
      const actions = h("div", { class: "card-actions" });
      if (kind === "context_full" && this.mode === "chat" && this.actions.newConversation) {
        const btn = h("button", { class: "btn btn-sm btn-primary", type: "button", text: "Start a new conversation" });
        btn.addEventListener("click", () => this.actions.newConversation(btn));
        actions.appendChild(btn);
      }
      if (needsSettings(kind, message)) actions.appendChild(settingsButton("Open Settings"));
      if ((kind === "model_unavailable" || kind === "rate_limited" || kind === "no_api_key") && this.actions.switchModel) {
        actions.appendChild(h("button", { class: "btn btn-sm", type: "button", text: "Switch model", onclick: () => this.actions.switchModel() }));
      }
      return h("div", { class: "error-card", role: "alert" },
        h("div", { class: "error-card-title" }, icon("alert"), ERROR_TITLES[kind] || ERROR_TITLES.other),
        h("p", { text: message }),
        actions.childElementCount ? actions : null);
    }

    renderAgentStatus(d) {
      if (this.mode !== "chat") return;
      if (d.state === "creating") return; // setup steps belong to the onboarding screen
      if (d.state === "error") {
        this.add(h("div", { class: "notice notice-error" }, icon("alert"), h("span", { text: d.message || "Agent setup failed." }),
          needsSettings(null, d.message) ? h("a", { class: "notice-action", href: "#/settings", text: "Open Settings" }) : null));
        return;
      }
      const message = d.message || (d.state === "ready" ? "The agent is ready." : `Agent ${d.state}.`);
      this.add(h("div", { class: "divider divider-muted" }, h("span", { text: message })));
    }

    renderJobRunCard(kind, d, at) {
      if (this.mode !== "chat") return;
      const href = `#/agent/${enc(this.agentId)}/jobs?run=${enc(d.run_id)}`;
      const name = d.job_name || "Job";
      if (kind === "job_run_started") {
        const card = h("a", { class: "run-card run-card-active", href },
          icon("clock", "run-card-icon"),
          h("div", { class: "run-card-body" },
            h("div", { class: "run-card-title" }, h("span", { text: `Job “${name}” started` }),
              d.late ? badge("late", "warn") : null,
              d.trigger === "manual" ? badge("manual", "muted") : null),
            h("div", { class: "run-card-text", text: "Running now. Open it to watch." })),
          spinner());
        this.jobRunCards.set(d.run_id, card);
        this.add(card);
        return;
      }
      // Finished: drop the "started" card and post the result where the user will see it next.
      const started = this.jobRunCards.get(d.run_id);
      if (started) { started.remove(); this.jobRunCards.delete(d.run_id); }
      const text = d.status === "succeeded" ? d.summary || "Finished." : d.error_message || d.summary || "";
      this.add(h("a", { class: `run-card run-card-${STATUS_TONES[d.status] || "muted"}`, href },
        icon("clock", "run-card-icon"),
        h("div", { class: "run-card-body" },
          h("div", { class: "run-card-title" }, h("span", { text: `Job “${name}”` }), statusBadge(d.status),
            h("span", { class: "run-card-time", title: fmtDateTime(at), text: fmtClock(at) })),
          text ? h("div", { class: "run-card-text", text }) : null)));
    }

    /** Stop spinners on every unfinished item of a turn that has ended. */
    finishTurn(turnId, status) {
      const t = this.turn(turnId);
      for (const key of t.items) {
        const entry = this.items.get(key);
        if (entry && !entry.done) entry.finish(status === "completed" ? "done" : "stopped");
      }
      if (t.activity) t.activity.finish();
    }

    /** Called when the server says nothing is running: no item can still be in progress. */
    finishDangling() {
      for (const entry of this.items.values()) {
        if (!entry.done) entry.finish("unfinished");
      }
      for (const t of this.turns.values()) if (t.activity && t.activity.running) t.activity.finish();
    }
  }

  // ======================================================================
  // 8. Agent page: header, settings dialog, tabs
  // ======================================================================

  async function viewAgent(root, token, agentId, tab, query) {
    const session = ensureSession(agentId);
    root.appendChild(loadingRow("Opening agent…"));
    // Only for model labels in the header; the page opens without it.
    const providersLoaded = loadProviders().catch(() => null);
    try {
      await session.ready;
      await providersLoaded;
    } catch (err) {
      if (!isCurrent(token)) return;
      closeSession(); // a later visit retries from scratch
      setChildren(root, err.status === 404
        ? h("div", { class: "card narrow center" },
          h("h1", { text: "Agent not found" }),
          h("p", { class: "muted", text: "This agent does not exist, or it belongs to another account." }),
          h("a", { class: "btn btn-primary", href: "#/agents", text: "Go to your agents" }))
        : errorBlock(err.message));
      return;
    }
    if (!isCurrent(token) || state.session !== session) return;

    const agent = session.agent;
    document.title = `${agent.name} · AgentWaiter`;
    const page = h("div", { class: "agent-page" }, agentHeader(session));
    setChildren(root, page);

    if (agent.setup_state !== "ready") {
      const host = h("div");
      page.appendChild(h("div", { class: "card narrow" },
        h("h2", { text: agent.setup_state === "error" ? "Setup needs attention" : "Setting up your agent" }),
        h("p", { class: "muted", text: "Chat, files and jobs open once setup finishes." }),
        host));
      mountSetupProgress(host, session);
      return;
    }

    page.appendChild(agentTabs(agentId, tab));
    const body = h("div", { class: "tab-body" });
    page.appendChild(body);
    if (tab === "chat") mountChatTab(body, session);
    else if (tab === "files") mountFilesTab(body, session, query, token);
    else mountJobsTab(body, session, query, token);
  }

  function agentTabs(agentId, active) {
    const tabs = [["chat", "Chat"], ["files", "Files"], ["jobs", "Jobs"]];
    return h("nav", { class: "tabs", "aria-label": "Agent sections" }, tabs.map(([key, label]) =>
      h("a", {
        class: "tab" + (key === active ? " active" : ""),
        href: `#/agent/${enc(agentId)}/${key}`,
        "aria-current": key === active ? "page" : null,
        text: label,
      })));
  }

  function connIndicator(connState) {
    const labels = { live: "Live", connecting: "Connecting…", reconnecting: "Reconnecting…", offline: "Offline, retrying…" };
    return h("span", { class: `conn conn-${connState}`, title: "Live updates from the agent" },
      h("i", { "aria-hidden": "true" }), labels[connState] || connState);
  }

  function agentHeader(session) {
    const titleRow = h("div", { class: "agent-title-row" });
    const meta = h("div", { class: "agent-meta" });
    const actions = h("div", { class: "agent-actions" });
    const head = h("div", { class: "agent-head" },
      h("div", { class: "agent-head-main" },
        h("a", { class: "back-link", href: "#/agents" }, icon("back"), "Agents"),
        titleRow,
        meta),
      actions);

    const actionButton = (action, label, iconName, extraClass) => {
      const btn = h("button", { class: `btn btn-sm ${extraClass || ""}`, type: "button" }, icon(iconName), label);
      btn.addEventListener("click", () => containerAction(session, action, btn));
      return btn;
    };

    const render = () => {
      const a = session.agent;
      if (!a) return;
      setChildren(titleRow, avatar(a.name, session.agentId), h("h1", { class: "agent-name", text: a.name }));
      setChildren(meta, 
        h("span", { class: "meta-item", text: agentModelText(a) }),
        a.setup_state === "ready" ? containerBadge(a.container) : null,
        session.busy ? badge("Working", "info", true) : null,
        connIndicator(session.connState));
      const buttons = [];
      if (a.setup_state === "ready") {
        if (a.container === "running") buttons.push(actionButton("stop", "Stop", "stop", "btn-ghost"));
        else buttons.push(actionButton("start", "Start", "play", "btn-ghost"));
        buttons.push(actionButton("restart", "Restart", "restart", "btn-ghost"));
      }
      const settingsBtn = h("button", { class: "btn btn-sm", type: "button", onclick: () => openSettings(session) }, icon("settings"), "Agent settings");
      buttons.push(settingsBtn);
      setChildren(actions, ...buttons);
    };
    render();
    onUnmount(session.onChange(render));
    return head;
  }

  async function containerAction(session, action, button) {
    if (action === "restart" && !window.confirm(
      "Restart this agent's container?\n\nAnything running stops, and software installed outside /workspace is removed. Files, the conversation and jobs are kept.")) return;
    if (action === "stop" && session.busy && !window.confirm("The agent is working on a message. Stopping the container stops that work. Continue?")) return;
    await withBusy(button, async () => {
      try {
        const res = await api("POST", agentUrl(session.agentId, `/container/${action}`));
        session.setAgent(res.agent);
        const done = { start: "Container started.", stop: "Container stopped.", restart: "Container restarted." };
        toast(done[action], "ok");
      } catch (err) {
        showError(err);
      }
    });
  }

  async function newConversation(session, button) {
    await withBusy(button, async () => {
      try {
        const res = await api("POST", agentUrl(session.agentId, "/conversation/new"));
        session.setAgent(res.agent);
      } catch (err) {
        showError(err);
      }
    });
  }

  /** Option values for the model <select>: provider and model id together. */
  function choiceValue(provider, model) {
    return JSON.stringify([provider, model]);
  }

  /**
   * A <select> of every model the user may pick, one <optgroup> per provider. Models of
   * providers without a usable key are disabled, except the agent's current one.
   */
  function modelSelect(providers, current) {
    const select = h("select");
    let found = false;
    for (const p of pickerGroups(providers)) {
      const group = h("optgroup", { label: p.available ? p.label : `${p.label} (no key; add one in Settings)` });
      for (const m of p.models) {
        const isCurrent = p.id === current.provider && m.id === current.model;
        if (isCurrent) found = true;
        group.appendChild(h("option", {
          value: choiceValue(p.id, m.id),
          text: isCurrent && !p.available ? `${m.label} (current; no key)` : m.label,
          disabled: !p.available && !isCurrent,
          selected: isCurrent,
        }));
      }
      select.appendChild(group);
    }
    if (!found) {
      select.prepend(h("option", {
        value: choiceValue(current.provider, current.model),
        text: `${modelLabel(current.model, current.provider)} (no longer offered)`,
        selected: true,
      }));
    }
    return select;
  }

  async function openSettings(session, opts) {
    const a = session.agent;
    if (!a || session.settingsOpening) return;
    // Fresh, so a key added in another tab shows up; the cached list is the fallback.
    session.settingsOpening = true;
    let providers;
    try {
      providers = await loadProviders(true);
    } catch (err) {
      providers = state.providers;
      if (!providers) { showError(err); return; }
    } finally {
      session.settingsOpening = false;
    }
    if (state.session !== session) return; // the user left the agent while the list loaded
    const name = h("input", { type: "text", value: a.name, maxlength: 40, required: true });
    const model = modelSelect(providers, { provider: a.provider, model: a.model });
    const missingKeys = pickerGroups(providers).some((p) => !p.available);
    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save" });
    const cancel = h("button", { class: "btn btn-ghost", type: "button", text: "Cancel" });
    const del = h("button", { class: "btn btn-danger-ghost", type: "button" }, icon("trash"), "Delete agent");

    const form = h("form", { class: "stack" },
      h("div", { class: "dialog-head" }, h("h2", { text: "Agent settings" })),
      field("Name", name),
      h("label", { class: "field" },
        h("span", { class: "field-label", text: "Model" }),
        model,
        h("span", { class: "field-hint" }, `Now: ${agentModelText(a)}. A new model applies from the next message.`,
          missingKeys ? [" ", h("a", { href: "#/settings", text: "Add a key in Settings" }), " to unlock more."] : null)),
      error,
      h("div", { class: "form-actions" }, cancel, save),
      h("div", { class: "danger-zone" },
        h("div", null,
          h("strong", { text: "Delete this agent" }),
          h("p", { class: "muted small", text: "Removes its container, files, conversation and jobs for good." })),
        del));
    const dialog = h("dialog", { class: "dialog" }, form);
    document.body.appendChild(dialog);
    dialog.addEventListener("close", () => dialog.remove());
    cancel.addEventListener("click", () => dialog.close());
    onUnmount(() => { if (dialog.open) dialog.close(); });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      error.hidden = true;
      const body = {};
      const newName = name.value.trim();
      if (!newName) { error.textContent = "The name cannot be empty."; error.hidden = false; return; }
      if (newName !== a.name) body.name = newName;
      let provider = a.provider;
      let modelId = a.model;
      try {
        [provider, modelId] = JSON.parse(model.value);
      } catch (err) {
        console.error("Unreadable model choice", model.value, err);
      }
      if (provider !== a.provider || modelId !== a.model) {
        body.provider = provider;
        body.model = modelId;
      }
      if (!Object.keys(body).length) { dialog.close(); return; }
      withBusy(save, async () => {
        try {
          const res = await api("PATCH", agentUrl(session.agentId), body);
          session.setAgent(res.agent);
          dialog.close();
          toast("Settings saved.", "ok");
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
        }
      });
    });

    del.addEventListener("click", () => {
      if (!window.confirm(`Delete "${a.name}"?\n\nIts container, files, conversation and jobs are removed for good.`)) return;
      withBusy(del, async () => {
        try {
          await api("DELETE", agentUrl(session.agentId));
          dialog.close();
          if (state.session === session) closeSession();
          toast(`Deleted ${a.name}.`, "ok");
          go("#/agents");
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
        }
      });
    });

    dialog.showModal();
    (opts && opts.focusModel ? model : name).focus();
  }

  // ======================================================================
  // 9. Chat tab
  // ======================================================================

  function mountChatTab(body, session) {
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
    const modKey = isMac ? "⌘" : "Ctrl";
    const convLabel = h("span", { class: "conv-label" });
    const newConvBtn = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, icon("plus"), "New conversation");
    newConvBtn.addEventListener("click", () => newConversation(session, newConvBtn));
    const input = h("textarea", {
      class: "composer-input",
      rows: 1,
      maxlength: 20000,
      placeholder: `Message ${session.agent.name}…`,
      "aria-label": "Message",
    });
    input.value = session.draft;
    // While the agent works the composer stays open: Send becomes Queue, and
    // "Interrupt & send" stops the current task and runs the new message next.
    const sendLabel = h("span", { text: "Send" });
    const sendBtn = h("button", { class: "btn btn-primary composer-send", type: "submit" }, icon("send"), sendLabel);
    const interruptBtn = h("button", {
      class: "btn composer-interrupt", type: "button", title: `Stop the current task and send this next (${modKey}+Enter)`,
    }, icon("play"), h("span", { text: "Interrupt & send" }));
    const stopLabel = h("span", { text: "Stop" });
    const stopBtn = h("button", { class: "btn composer-stop", type: "button", title: "Stop the current task. Queued messages still run." }, icon("stop"), stopLabel);
    const form = h("form", { class: "composer" }, input, h("div", { class: "composer-actions" }, stopBtn, interruptBtn, sendBtn));
    const hint = h("div", { class: "composer-hint" });
    const queueList = h("ol", { class: "queue-list" });
    const queueCount = h("span", { class: "queue-title" });
    const clearBtn = h("button", { class: "link-btn queue-clear", type: "button", text: "Clear queue" });
    const queuePanel = h("div", { class: "queue-panel", hidden: true, "aria-label": "Queued messages" },
      h("div", { class: "queue-head" }, queueCount, clearBtn),
      queueList);
    const panel = h("div", { class: "chat-panel" },
      h("div", { class: "chat-toolbar" }, convLabel, newConvBtn),
      session.timeline.root,
      queuePanel,
      form,
      hint);
    body.appendChild(panel);

    let sending = false;
    let wasBusy = session.busy;
    let queueKey = null; // what the queue panel last drew, to skip needless rebuilds

    const renderQueue = () => {
      const items = session.queue;
      const key = items.map((q) => `${q.turn_id}:${q.priority}`).join(",");
      if (key === queueKey) return;
      queueKey = key;
      queuePanel.hidden = !items.length;
      queueCount.textContent = `Queued · ${items.length}`;
      clearBtn.hidden = items.length < 2;
      setChildren(queueList, ...items.map((q) => {
        const oneLine = q.prompt.replace(/\s+/g, " ").trim();
        const cancel = h("button", {
          class: "queue-cancel", type: "button", title: "Remove from the queue", "aria-label": "Remove from the queue",
        }, icon("x"));
        cancel.addEventListener("click", () => cancelQueued(q.turn_id, cancel));
        return h("li", { class: "queue-item" },
          q.priority === 1 ? badge("next", "info") : null,
          h("span", { class: "queue-text", title: q.prompt, text: oneLine }),
          cancel);
      }));
    };

    const update = () => {
      if (!session.agent) return;
      convLabel.textContent = `Conversation ${session.agent.conversation_no || 1}`;
      const busy = session.busy;
      const waiting = busy || session.queue.length > 0; // a new message would wait
      // Hand the keyboard back when the agent finishes, so the user can reply straight away.
      if (wasBusy && !busy && document.activeElement !== input) requestAnimationFrame(() => input.focus());
      wasBusy = busy;
      sendLabel.textContent = waiting ? "Queue" : "Send";
      sendBtn.title = waiting ? "Run this after the current task (Enter)" : "Send (Enter)";
      interruptBtn.hidden = !busy;
      stopBtn.hidden = !busy;
      stopBtn.disabled = session.stopping;
      stopLabel.textContent = session.stopping ? "Stopping…" : "Stop";
      input.disabled = sending;
      sendBtn.disabled = sending;
      interruptBtn.disabled = sending || session.stopping;
      newConvBtn.disabled = busy;
      input.placeholder = busy ? `Message ${session.agent.name}… (it runs when the current task finishes)` : `Message ${session.agent.name}…`;
      hint.textContent = busy
        ? `Enter to queue · ${modKey}+Enter to interrupt and send · Shift+Enter for a new line`
        : "Enter to send · Shift+Enter for a new line";
      renderQueue();
    };

    const autosize = () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
    };

    async function send(mode) {
      const text = input.value;
      if (!text.trim() || sending) return;
      const interrupt = mode === "interrupt" && session.busy;
      sending = true;
      update();
      try {
        const res = await api("POST", agentUrl(session.agentId, "/messages"), { text, mode: interrupt ? "interrupt" : "queue" });
        session.draft = "";
        input.value = "";
        autosize();
        if (res.queued) {
          // It shows in the timeline only once it starts (turn_started); until then it waits
          // in the queue panel. The turn_queued event may already have added it.
          session.enqueue({ turn_id: res.turn_id, prompt: text, priority: interrupt ? 1 : 0, queued_at: new Date().toISOString() });
          if (interrupt && session.busy) session.setStopping(true);
          else session.notify();
        } else {
          session.setBusy(true, res.turn_id);
          session.timeline.addUserMessage(res.turn_id, text);
        }
      } catch (err) {
        // For example 409 "The queue is full (10 messages)…": the server's text says what to do.
        showError(err);
      } finally {
        sending = false;
        update();
        input.focus();
      }
    }

    async function cancelQueued(turnId, button) {
      await withBusy(button, async () => {
        try {
          await api("DELETE", agentUrl(session.agentId, `/queue/${enc(turnId)}`));
        } catch (err) {
          // 404: it already started (its turn_started is on the way) or was cancelled elsewhere.
          if (err.status !== 404) { showError(err); return; }
        }
        session.dequeue(turnId);
      });
    }

    async function clearQueue() {
      await withBusy(clearBtn, async () => {
        try {
          await api("DELETE", agentUrl(session.agentId, "/queue"));
        } catch (err) {
          showError(err);
          return;
        }
        // turn_cancelled events remove each one; drop them now so the panel reacts at once.
        for (const q of session.queue.slice()) session.dequeue(q.turn_id);
      });
    }

    async function stop() {
      session.setStopping(true);
      try {
        const res = await api("POST", agentUrl(session.agentId, "/stop"));
        if (res && res.stopped === false) {
          // Nothing was running on the server; our busy state was stale.
          session.setBusy(false, null);
          session.timeline.finishDangling();
        }
      } catch (err) {
        session.setStopping(false);
        showError(err);
      }
    }

    form.addEventListener("submit", (e) => { e.preventDefault(); send("queue"); });
    interruptBtn.addEventListener("click", () => send("interrupt"));
    stopBtn.addEventListener("click", stop);
    clearBtn.addEventListener("click", clearQueue);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send(e.ctrlKey || e.metaKey ? "interrupt" : "queue");
      }
    });
    input.addEventListener("input", () => {
      session.draft = input.value;
      autosize();
    });

    // The chat panel fills the window below the header so the timeline scrolls, not the page.
    const fit = () => {
      const top = panel.getBoundingClientRect().top + window.scrollY;
      panel.style.height = `${Math.max(380, window.innerHeight - top - 12)}px`;
    };
    window.addEventListener("resize", fit);
    onUnmount(() => window.removeEventListener("resize", fit));
    onUnmount(session.onChange(update));
    update();
    autosize();
    requestAnimationFrame(() => {
      fit();
      session.timeline.scrollToBottom();
      if (!input.disabled) input.focus();
    });
  }

  // ======================================================================
  // 10. Files tab
  // ======================================================================

  function filesHash(agentId, path, open) {
    const q = new URLSearchParams();
    if (path) q.set("path", path);
    if (open) q.set("open", open);
    const s = q.toString();
    return `#/agent/${enc(agentId)}/files${s ? "?" + s : ""}`;
  }

  function downloadUrl(agentId, path, inline) {
    return withQuery(agentUrl(agentId, "/files/download"), inline ? { path, inline: 1, v: Date.now() } : { path });
  }

  function mountFilesTab(body, session, query, token) {
    const agentId = session.agentId;
    const dir = (query.get("path") || "").replace(/^\/+|\/+$/g, "");
    let current = null; // path of the file open in the preview pane

    const crumbs = h("nav", { class: "breadcrumb", "aria-label": "Folder" });
    const fileInput = h("input", { type: "file", multiple: true, hidden: true });
    const uploadBtn = h("button", { class: "btn btn-sm", type: "button" }, icon("upload"), "Upload");
    const mkdirBtn = h("button", { class: "btn btn-sm btn-ghost", type: "button" }, icon("folder"), "New folder");
    const refreshBtn = h("button", { class: "btn btn-sm btn-ghost btn-icon", type: "button", title: "Refresh", "aria-label": "Refresh" }, icon("refresh"));
    const tableBody = h("tbody");
    const listCard = h("div", { class: "card file-list" },
      h("table", { class: "file-table" },
        h("thead", null, h("tr", null,
          h("th", { text: "Name" }),
          h("th", { class: "col-size", text: "Size" }),
          h("th", { class: "col-mod", text: "Modified" }),
          h("th", { class: "col-actions" }, h("span", { class: "sr-only", text: "Actions" })))),
        tableBody),
      h("div", { class: "drop-hint", text: "Drop files to upload them here" }));
    const preview = h("aside", { class: "card file-preview", hidden: true, "aria-label": "File preview" });
    body.append(
      h("div", { class: "files-toolbar" }, crumbs, h("div", { class: "toolbar-actions" }, uploadBtn, mkdirBtn, refreshBtn, fileInput)),
      h("div", { class: "files-layout" }, listCard, preview));

    // ---- breadcrumb ----
    const parts = dir ? dir.split("/") : [];
    crumbs.append(parts.length
      ? h("a", { href: filesHash(agentId, ""), text: "Workspace" })
      : h("span", { class: "crumb-current", "aria-current": "page", text: "Workspace" }));
    parts.forEach((part, i) => {
      crumbs.append(h("span", { class: "crumb-sep", "aria-hidden": "true", text: "/" }));
      const path = parts.slice(0, i + 1).join("/");
      crumbs.append(i === parts.length - 1
        ? h("span", { class: "crumb-current", "aria-current": "page", text: part })
        : h("a", { href: filesHash(agentId, path), text: part }));
    });

    // ---- listing ----
    async function load() {
      let res;
      try {
        res = await api("GET", withQuery(agentUrl(agentId, "/files"), { path: dir }));
      } catch (err) {
        if (!isCurrent(token)) return;
        if (err.status === 409 && dir) {
          // The path is a file, not a folder: open its folder with the file previewed.
          redirect(filesHash(agentId, dirname(dir), dir));
          return;
        }
        const msg = err.status === 404 ? "This folder does not exist. It may have been deleted. " : err.message + " ";
        setChildren(tableBody, h("tr", null, h("td", { class: "empty-cell", colspan: 4 }, msg,
          err.status === 404 ? h("a", { href: filesHash(agentId, ""), text: "Go to the workspace root" }) : null)));
        return;
      }
      if (!isCurrent(token)) return;
      const entries = res.entries || [];
      if (!entries.length) {
        setChildren(tableBody, h("tr", null, h("td", { class: "empty-cell", colspan: 4,
          text: dir ? "This folder is empty." : "The workspace is empty. Upload files, or ask the agent to create some." })));
        return;
      }
      setChildren(tableBody, ...entries.map(entryRow));
    }

    function entryRow(e) {
      const iconName = e.type === "dir" ? "folder" : e.type === "symlink" ? "link" : "file";
      const row = h("tr", {
        class: "file-row" + (e.path === current ? " selected" : ""),
        tabindex: 0,
        dataset: { path: e.path },
        onclick: () => openEntry(e),
        onkeydown: (ev) => { if (ev.key === "Enter") openEntry(e); },
      },
      h("td", { class: "col-name" }, h("span", { class: "name-cell" },
        icon(iconName, `file-icon file-icon-${e.type}`),
        h("span", { class: "file-name", text: e.name }),
        e.type === "symlink" ? badge("link", "muted") : null)),
      h("td", { class: "col-size", text: e.type === "file" ? fmtSize(e.size) : "" }),
      h("td", { class: "col-mod" }, e.modified !== undefined && e.modified !== null ? timeEl(e.modified) : ""),
      h("td", { class: "col-actions" },
        e.type === "file" ? downloadLink(e.path, true) : null,
        deleteButton(e.path, e.type, true)));
      return row;
    }

    function markSelected() {
      for (const tr of tableBody.querySelectorAll("tr.file-row")) tr.classList.toggle("selected", tr.dataset.path === current);
    }

    function openEntry(e) {
      if (e.type === "dir") go(filesHash(agentId, e.path));
      else if (e.type === "file") showPreview(e.path, false);
      else if (e.type === "symlink") toast("This is a link the agent created. AgentWaiter never follows links, so it cannot be opened here. You can delete it.", "info");
      else toast("This is a special file and cannot be opened.", "info");
    }

    function downloadLink(path, compact) {
      const name = basename(path);
      return h("a", {
        class: compact ? "btn btn-sm btn-ghost btn-icon" : "btn btn-sm",
        href: downloadUrl(agentId, path, false),
        download: name,
        title: `Download ${name}`,
        "aria-label": `Download ${name}`,
        onclick: (ev) => ev.stopPropagation(),
      }, icon("download"), compact ? null : "Download");
    }

    function deleteButton(path, type, compact) {
      const name = basename(path);
      const btn = h("button", {
        class: compact ? "btn btn-sm btn-ghost btn-icon btn-danger-ghost" : "btn btn-sm btn-danger-ghost",
        type: "button",
        title: `Delete ${name}`,
        "aria-label": `Delete ${name}`,
      }, icon("trash"), compact ? null : "Delete");
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const question = type === "dir" ? `Delete the folder "${name}" and everything in it?` : `Delete "${name}"?`;
        if (!window.confirm(question)) return;
        withBusy(btn, async () => {
          try {
            await api("DELETE", withQuery(agentUrl(agentId, "/files"), { path }));
            toast(`Deleted ${name}.`, "ok");
            if (current && (current === path || current.startsWith(path + "/"))) closePreview();
            await load();
          } catch (err) {
            showError(err);
          }
        });
      });
      return btn;
    }

    // ---- preview ----
    function closePreview() {
      current = null;
      preview.hidden = true;
      preview.replaceChildren();
      markSelected();
    }

    async function showPreview(path, quiet) {
      current = path;
      markSelected();
      const name = basename(path);
      const content = h("div", { class: "preview-body" }, loadingRow("Loading preview…"));
      if (!quiet || preview.hidden) {
        preview.hidden = false;
        setChildren(preview, 
          h("div", { class: "preview-head" },
            h("div", { class: "preview-title" }, h("strong", { text: name }), h("span", { class: "muted small mono", text: path })),
            h("button", { class: "btn btn-sm btn-ghost btn-icon", type: "button", title: "Close preview", "aria-label": "Close preview", onclick: closePreview }, icon("x"))),
          h("div", { class: "preview-actions" }, downloadLink(path, false), deleteButton(path, "file", false)),
          content);
      }
      let res;
      try {
        res = await api("GET", withQuery(agentUrl(agentId, "/files/preview"), { path }));
      } catch (err) {
        if (current !== path || !isCurrent(token)) return;
        if (quiet && err.status === 404) { closePreview(); return; } // deleted since
        setChildren(content, errorBlock(err.message));
        swapPreviewBody(content);
        return;
      }
      if (current !== path || !isCurrent(token)) return;
      const sizeNote = h("div", { class: "muted small", text: fmtSize(res.size) });
      if (res.kind === "text") {
        setChildren(content, sizeNote, h("pre", { class: "preview-text", text: res.text || "" }),
          res.truncated ? h("p", { class: "muted small", text: "Showing the first 1 MiB. Download the file to see all of it." }) : null);
      } else if (res.kind === "image") {
        setChildren(content, sizeNote, h("img", { class: "preview-image", alt: name, src: downloadUrl(agentId, path, true) }));
      } else {
        setChildren(content, sizeNote, h("div", { class: "empty" },
          h("p", { text: "This file cannot be shown in the browser." }), downloadLink(path, false)));
      }
      swapPreviewBody(content);
    }

    // A quiet refresh builds the new body off-screen, then swaps it in to avoid a flash.
    function swapPreviewBody(content) {
      const old = preview.querySelector(".preview-body");
      if (old && old !== content) old.replaceWith(content);
    }

    // ---- actions ----
    async function upload(files) {
      if (!files.length) return;
      const maxMb = limits().max_upload_mb;
      if (typeof maxMb === "number") {
        const tooBig = files.find((f) => f.size > maxMb * 1024 * 1024);
        if (tooBig) { toast(`"${tooBig.name}" is larger than the ${maxMb} MB upload limit.`, "error"); return; }
      }
      const form = new FormData();
      for (const f of files) form.append("files", f, f.name);
      await withBusy(uploadBtn, async () => {
        try {
          const res = await api("POST", withQuery(agentUrl(agentId, "/files/upload"), { path: dir }), form);
          const n = (res && res.saved && res.saved.length) || files.length;
          toast(`Uploaded ${n} file${n === 1 ? "" : "s"}.`, "ok");
          await load();
        } catch (err) {
          showError(err);
        }
      });
    }

    async function makeFolder() {
      const raw = window.prompt("Name of the new folder");
      if (raw === null) return;
      const name = raw.trim();
      if (!name) return;
      if (name.includes("/") || name === "." || name === "..") { toast("A folder name cannot contain / or be . or ..", "error"); return; }
      await withBusy(mkdirBtn, async () => {
        try {
          await api("POST", agentUrl(agentId, "/files/mkdir"), { path: joinPath(dir, name) });
          await load();
        } catch (err) {
          showError(err);
        }
      });
    }

    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const files = Array.from(fileInput.files || []);
      fileInput.value = "";
      upload(files);
    });
    mkdirBtn.addEventListener("click", makeFolder);
    refreshBtn.addEventListener("click", () => { load(); if (current) showPreview(current, true); });

    // Drag and drop onto the listing uploads into the current folder.
    listCard.addEventListener("dragover", (e) => {
      if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
      e.preventDefault();
      listCard.classList.add("dragging");
    });
    listCard.addEventListener("dragleave", (e) => {
      if (!listCard.contains(e.relatedTarget)) listCard.classList.remove("dragging");
    });
    listCard.addEventListener("drop", (e) => {
      e.preventDefault();
      listCard.classList.remove("dragging");
      upload(Array.from((e.dataTransfer && e.dataTransfer.files) || []));
    });

    // Agents change files with commands and patches: refresh when one finishes.
    let refreshTimer = null;
    const off = session.on((ev) => {
      if (ev.kind !== "item_completed" || !ev.data) return;
      if (ev.data.item_type !== "file_change" && ev.data.item_type !== "command") return;
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => {
        load();
        if (current) showPreview(current, true);
      }, 500);
    });
    onUnmount(() => { off(); clearTimeout(refreshTimer); });

    tableBody.appendChild(h("tr", null, h("td", { colspan: 4 }, loadingRow("Loading files…"))));
    load();
    const openPath = query.get("open");
    if (openPath) showPreview(openPath, false);
  }

  // ======================================================================
  // 11. Jobs tab (job list, create form, runs, run transcript)
  // ======================================================================

  function runHash(agentId, runId) {
    return `#/agent/${enc(agentId)}/jobs?run=${enc(runId)}`;
  }

  const RUN_STATUS_TEXT = {
    queued: "Waiting to start.",
    running: "Running now.",
    succeeded: "Finished.",
    failed: "Failed.",
    interrupted: "Interrupted.",
    skipped: "Skipped: the previous run was still going.",
    missed: "Missed.",
  };

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function localInputValue(date) {
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}T${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }

  function mountJobsTab(body, session, query, token) {
    const runId = query.get("run");
    if (runId) { mountRunView(body, session, runId, token); return; }

    const agentId = session.agentId;
    const listEl = h("div", { class: "job-list" }, loadingRow("Loading jobs…"));
    const formCard = jobForm(agentId, () => load());
    const refreshBtn = h("button", { class: "btn btn-sm btn-ghost btn-icon", type: "button", title: "Refresh", "aria-label": "Refresh jobs", onclick: () => load() }, icon("refresh"));
    body.append(formCard, h("div", { class: "section-head" }, h("h2", { text: "Jobs" }), refreshBtn), listEl);

    let loadGen = 0;
    async function load() {
      const gen = ++loadGen;
      let res;
      try {
        res = await api("GET", agentUrl(agentId, "/jobs"));
      } catch (err) {
        if (isCurrent(token) && gen === loadGen) setChildren(listEl, errorBlock(err.message));
        return;
      }
      if (!isCurrent(token) || gen !== loadGen) return;
      const jobs = res.jobs || [];
      if (!jobs.length) {
        formCard.open = true; // nothing to list yet, so lead with the form
        setChildren(listEl, emptyBlock("No jobs yet. Create one above, or ask the agent in chat to schedule something."));
        return;
      }
      setChildren(listEl, ...jobs.map(jobCard));
    }

    function jobCard(job) {
      const expanded = session.expandedJobs.has(job.id);
      const runsEl = h("div", { class: "job-runs", hidden: !expanded });
      const toggle = h("button", { class: "btn btn-sm btn-ghost", type: "button", "aria-expanded": String(expanded), text: expanded ? "Hide runs" : "Show runs" });
      toggle.addEventListener("click", () => {
        const open = runsEl.hidden;
        runsEl.hidden = !open;
        toggle.textContent = open ? "Hide runs" : "Show runs";
        toggle.setAttribute("aria-expanded", String(open));
        if (open) { session.expandedJobs.add(job.id); loadRuns(job, runsEl); } else session.expandedJobs.delete(job.id);
      });
      const actions = h("div", { class: "job-actions" });
      if (job.status === "active") {
        const runBtn = h("button", { class: "btn btn-sm", type: "button" }, icon("play"), "Run now");
        runBtn.addEventListener("click", () => runNow(job, runBtn));
        const cancelBtn = h("button", { class: "btn btn-sm btn-danger-ghost", type: "button" }, icon("x"), "Cancel");
        cancelBtn.addEventListener("click", () => cancelJob(job, cancelBtn));
        actions.append(runBtn, cancelBtn);
      }
      actions.append(toggle);

      const last = job.last_run;
      const schedule = job.schedule || {};
      const card = h("article", { class: `card job-card job-${job.status}` },
        h("div", { class: "job-top" },
          h("div", { class: "job-title" },
            h("h3", { text: job.name }),
            job.created_by === "agent" ? badge("by the agent", "agent") : badge("by you", "muted")),
          statusBadge(job.status)),
        // describe_schedule wraps cron expressions in backticks; show them as inline code.
        h("div", { class: "job-schedule" }, icon("clock"), appendAll(h("span"), [richInline(schedule.description || schedule.value || "")])),
        h("dl", { class: "facts" },
          h("div", null, h("dt", { text: "Next run" }), h("dd", null,
            job.next_run_at
              ? [fmtDateTime(job.next_run_at), " ", h("span", { class: "muted", text: `(${fmtRelative(job.next_run_at)})` })]
              : "—")),
          h("div", null, h("dt", { text: "Last result" }), h("dd", null, last ? lastResult(last) : h("span", { class: "muted", text: "Not run yet" })))),
        h("details", { class: "job-prompt" }, h("summary", { text: "Prompt" }), h("p", { text: job.prompt })),
        actions,
        runsEl);
      if (expanded) loadRuns(job, runsEl);
      return card;
    }

    function lastResult(run) {
      return h("div", { class: "last-result" },
        statusBadge(run.status),
        run.late ? badge("late", "warn") : null,
        h("a", { class: "last-summary", href: runHash(agentId, run.id), text: run.error_message || run.summary || RUN_STATUS_TEXT[run.status] || "" }));
    }

    async function loadRuns(job, el) {
      if (!el.childElementCount) setChildren(el, loadingRow("Loading runs…"));
      let res;
      try {
        res = await api("GET", withQuery(agentUrl(agentId, `/jobs/${enc(job.id)}/runs`), { limit: 20 }));
      } catch (err) {
        if (isCurrent(token)) setChildren(el, errorBlock(err.message));
        return;
      }
      if (!isCurrent(token)) return;
      const runs = res.runs || [];
      if (!runs.length) { setChildren(el, h("p", { class: "muted small", text: "No runs yet." })); return; }
      setChildren(el, ...runs.map((r) => h("a", { class: "run-row", href: runHash(agentId, r.id) },
        h("span", { class: "run-row-status" }, statusBadge(r.status), r.late ? badge("late", "warn") : null, r.trigger === "manual" ? badge("manual", "muted") : null),
        h("span", { class: "run-row-when", title: `Scheduled for ${fmtDateTime(r.scheduled_for)}`, text: fmtDateTime(r.started_at || r.scheduled_for) }),
        h("span", { class: "run-row-summary", text: r.error_message || r.summary || RUN_STATUS_TEXT[r.status] || "" }),
        h("span", { class: "run-row-dur", text: between(r.started_at, r.finished_at) }))));
    }

    async function runNow(job, btn) {
      await withBusy(btn, async () => {
        try {
          await api("POST", agentUrl(agentId, `/jobs/${enc(job.id)}/run`));
          toast(`Started a run of “${job.name}”.`, "ok");
          session.expandedJobs.add(job.id);
          await load();
        } catch (err) {
          showError(err);
        }
      });
    }

    async function cancelJob(job, btn) {
      if (!window.confirm(`Cancel the job "${job.name}"?\n\nIt will not run again, and a run in progress is stopped.`)) return;
      await withBusy(btn, async () => {
        try {
          await api("DELETE", agentUrl(agentId, `/jobs/${enc(job.id)}`));
          toast(`Cancelled “${job.name}”.`, "ok");
          await load();
        } catch (err) {
          showError(err);
        }
      });
    }

    // Live refresh on job events, plus a slow tick so "in 5 minutes" stays true.
    let refreshTimer = null;
    const off = session.on((ev) => {
      if (!["jobs_changed", "job_run_started", "job_run_finished"].includes(ev.kind)) return;
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(load, 400);
    });
    const tick = setInterval(load, 60000);
    onUnmount(() => { off(); clearTimeout(refreshTimer); clearInterval(tick); });
    load();
  }

  function jobForm(agentId, onCreated) {
    const tz = userTimezone();
    const minInterval = limits().min_job_interval_minutes || 5;
    const name = h("input", { type: "text", maxlength: 80, required: true, placeholder: "e.g. Morning briefing" });
    const prompt = h("textarea", {
      rows: 3, maxlength: 8000, required: true,
      placeholder: "What should the agent do each time? Each run starts a fresh conversation, so include everything it needs.",
    });
    const inAnHour = new Date(Date.now() + 3600 * 1000);
    inAnHour.setMinutes(0, 0, 0);
    const at = h("input", { type: "datetime-local", value: localInputValue(inAnHour), min: localInputValue(new Date()) });
    const minutes = h("input", { type: "number", min: minInterval, max: 525600, step: 1, value: Math.max(60, minInterval) });
    const cron = h("input", { type: "text", placeholder: "0 9 * * 1-5", spellcheck: "false", autocomplete: "off", class: "mono" });

    const panels = {
      once: field("When", at, `Your local time (${tz}).`),
      interval: field("Every how many minutes", minutes, `At least ${minInterval} minutes. The first run is one interval from now.`),
      cron: field("Cron expression", cron, `minute hour day-of-month month day-of-week, evaluated in ${tz}. Runs must be at least ${minInterval} minutes apart.`),
    };
    let kind = "once";
    const kindRadios = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Schedule type" },
      [["once", "Once"], ["interval", "Repeat every N minutes"], ["cron", "Cron"]].map(([value, label]) => {
        const radio = h("input", { type: "radio", name: `job-kind-${agentId}`, value, checked: value === kind });
        radio.addEventListener("change", () => { kind = value; showPanel(); });
        return h("label", { class: "segment" }, radio, h("span", { text: label }));
      }));
    const panelHost = h("div");
    const showPanel = () => setChildren(panelHost, panels[kind]);
    showPanel();

    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("plus"), "Create job");
    const form = h("form", { class: "stack" },
      field("Name", name),
      field("Prompt", prompt),
      h("div", { class: "field" }, h("span", { class: "field-label", text: "Schedule" }), kindRadios),
      panelHost,
      error,
      h("div", { class: "form-actions" }, submit));

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      error.hidden = true;
      const fail = (msg) => { error.textContent = msg; error.hidden = false; };
      let schedule;
      if (kind === "once") {
        const when = at.value ? new Date(at.value) : null; // datetime-local is read as local time
        if (!when || isNaN(when.getTime())) return fail("Pick a date and time.");
        schedule = { kind: "once", at: when.toISOString() };
      } else if (kind === "interval") {
        const n = Number(minutes.value);
        if (!Number.isInteger(n) || n < minInterval) return fail(`Enter a whole number of minutes, at least ${minInterval}.`);
        schedule = { kind: "interval", minutes: n };
      } else {
        const expr = cron.value.trim();
        if (expr.split(/\s+/).length !== 5) return fail("A cron expression has five fields, for example 0 9 * * 1-5.");
        schedule = { kind: "cron", expr };
      }
      withBusy(submit, async () => {
        try {
          await api("POST", agentUrl(agentId, "/jobs"), { name: name.value.trim(), prompt: prompt.value.trim(), schedule, timezone: tz });
          toast("Job created.", "ok");
          name.value = "";
          prompt.value = "";
          onCreated();
        } catch (err) {
          fail(err.message);
        }
      });
    });

    return h("details", { class: "card job-form-card" },
      h("summary", { class: "card-summary" }, icon("plus"), "New job"),
      form);
  }

  function fact(label, value) {
    return h("div", null, h("dt", { text: label }), h("dd", null, value));
  }

  /** One job run: its details and the transcript of what the agent did, live while it runs. */
  function mountRunView(body, session, runId, token) {
    const agentId = session.agentId;
    const head = h("div", { class: "card run-head" }, loadingRow("Loading run…"));
    const transcriptHost = h("div", { class: "run-transcript" });
    body.append(
      h("a", { class: "back-link", href: `#/agent/${enc(agentId)}/jobs` }, icon("back"), "All jobs"),
      head,
      h("h2", { class: "section-title", text: "Transcript" }),
      transcriptHost);

    let tl = null;
    let run = null;
    let buffering = true;
    let buffer = [];
    let gen = 0;

    const turnOf = (ev) => (ev.kind === "snapshot" ? (ev.data && ev.data.turn_id) || ev.turn_id : ev.turn_id);

    // Subscribe before loading so nothing that happens during the request is lost.
    const off = session.on((ev) => {
      if (buffering) buffer.push(ev);
      else if (tl && run && run.turn_id && turnOf(ev) === run.turn_id) tl.apply(ev);
      if ((ev.kind === "job_run_started" || ev.kind === "job_run_finished") && ev.data && ev.data.run_id === runId) load();
    });
    onUnmount(off);

    async function load() {
      const my = ++gen;
      buffering = true;
      buffer = [];
      let res;
      let jobs = null;
      try {
        [res, jobs] = await Promise.all([
          api("GET", agentUrl(agentId, `/runs/${enc(runId)}`)),
          // Only for the job's name; the transcript does not depend on it.
          api("GET", agentUrl(agentId, "/jobs")).catch((err) => { console.warn("Could not load jobs for the run title", err); return null; }),
        ]);
      } catch (err) {
        if (my !== gen || !isCurrent(token)) return;
        buffering = false;
        setChildren(head, err.status === 404 ? errorBlock("This run does not exist.") : errorBlock(err.message));
        return;
      }
      if (my !== gen || !isCurrent(token)) return;
      run = res.run;
      const job = jobs && (jobs.jobs || []).find((j) => j.id === run.job_id);
      renderHead(run, job);

      const next = new Timeline({
        agentId,
        mode: "run",
        turnId: run.turn_id,
        emptyText: run.turn_id ? "Nothing has happened in this run yet." : "This run did not execute, so there is no transcript.",
        actions: { switchModel: () => openSettings(session, { focusModel: true }) },
      });
      for (const ev of res.events || []) next.apply(ev);
      // Text streamed so far lives only in memory; the session has collected it.
      const partial = run.turn_id && session.partials.get(run.turn_id);
      if (partial) next.applySnapshot(run.turn_id, Object.fromEntries(partial));
      // Persisted events that arrived during the request; buffered deltas are already in `partial`.
      for (const ev of buffer) if (ev.seq !== null && ev.seq !== undefined && turnOf(ev) === run.turn_id) next.apply(ev);
      buffer = [];
      buffering = false;
      const live = run.status === "running" || run.status === "queued";
      next.setWorking(live, run.status === "queued" ? "Waiting for a free slot…" : "Working…");
      if (!live) next.finishDangling();
      if (tl) tl.root.replaceWith(next.root);
      else transcriptHost.appendChild(next.root);
      tl = next;
    }

    function renderHead(r, job) {
      setChildren(head, 
        h("div", { class: "run-head-top" },
          h("h2", { text: job ? job.name : "Job run" }),
          statusBadge(r.status),
          r.late ? badge("late", "warn") : null,
          r.trigger === "manual" ? badge("manual", "muted") : null),
        h("dl", { class: "facts" },
          fact("Scheduled for", fmtDateTime(r.scheduled_for)),
          fact("Started", r.started_at ? fmtDateTime(r.started_at) : "—"),
          fact("Finished", r.finished_at ? fmtDateTime(r.finished_at) : "—"),
          fact("Duration", between(r.started_at, r.finished_at) || "—")),
        r.summary ? h("div", { class: "run-summary" }, h("div", { class: "field-label", text: "Summary" }), h("p", { text: r.summary })) : null,
        r.error_message ? errorBlock(r.error_message,
          needsSettings(r.error_kind, r.error_message) ? h("a", { class: "notice-action", href: "#/settings", text: "Open Settings" }) : null) : null);
    }

    load();
  }

  // ======================================================================
  // 12. Settings page: API keys, usage, account
  // ======================================================================

  const SETTINGS_TABS = [["keys", "API keys"], ["usage", "Usage"], ["account", "Account"]];

  async function viewSettings(root, token, tab) {
    document.title = "Settings · AgentWaiter";
    const body = h("div", { class: "settings-body" });
    root.appendChild(h("div", { class: "settings-page" },
      h("div", { class: "page-head" },
        h("div", null,
          h("h1", { text: "Settings" }),
          h("p", { class: "muted", text: "Your API keys, model usage and account." }))),
      h("nav", { class: "tabs", "aria-label": "Settings sections" }, SETTINGS_TABS.map(([key, label]) =>
        h("a", {
          class: "tab" + (key === tab ? " active" : ""),
          href: key === "keys" ? "#/settings" : `#/settings/${key}`,
          "aria-current": key === tab ? "page" : null,
          text: label,
        }))),
      body));
    if (tab === "keys") await mountKeysTab(body, token);
    else if (tab === "usage") await mountUsageTab(body, token);
    else mountAccountTab(body);
  }

  // ---- API keys -----------------------------------------------------------

  function agentsUsingText(n) {
    return n === 1 ? "1 of your agents uses it." : `${n} of your agents use it.`;
  }

  /** Swap a button's content for a spinner and `label` while fn runs. */
  async function withBusyLabel(button, label, fn) {
    const saved = Array.from(button.childNodes);
    setChildren(button, spinner(), label);
    try {
      return await withBusy(button, fn);
    } finally {
      button.replaceChildren(...saved);
    }
  }

  /** "a, b" from a comma-separated input, cleaned the way the server cleans it, for comparing. */
  function modelListText(value) {
    const list = Array.isArray(value) ? value : String(value || "").split(",");
    return list.map((m) => String(m).trim()).filter(Boolean).join(", ");
  }

  async function mountKeysTab(body, token) {
    const list = h("div", { class: "provider-list" }, loadingRow("Loading providers…"));
    body.append(
      h("p", { class: "keys-intro muted" },
        "Keys are encrypted on this server and never shown again after you save them. They are never given to your agents' containers: agents call models through this server, which adds your key to each request."),
      list);

    const render = () => {
      const providers = state.providers || [];
      if (!providers.length) {
        setChildren(list, emptyBlock("This server provides the models itself, so there are no keys to add."));
        return;
      }
      setChildren(list, ...providers.map((p) => (p.user_key ? providerCard(p) : platformCard(p))));
    };
    // After a key is removed, every card can change (counts, which providers are available).
    const reload = async () => {
      try {
        await loadProviders(true);
      } catch (err) {
        showError(err);
        return;
      }
      if (isCurrent(token)) render();
    };

    try {
      await loadProviders(true);
    } catch (err) {
      if (isCurrent(token)) setChildren(list, errorBlock(err.message));
      return;
    }
    if (isCurrent(token)) render();

    function platformCard(p) {
      return h("section", { class: "card provider-card" },
        h("div", { class: "provider-head" },
          h("h2", { text: p.label }),
          p.available ? badge("Included", "ok") : badge("Not available", "warn")),
        h("p", { class: "muted", text: p.description }),
        h("div", { class: "provider-facts" },
          h("p", { class: "provider-status" },
            p.available
              ? [icon("check"), h("span", { text: "Included: shared key on this server. Nothing to set up." })]
              : h("span", { text: "Your account may not use the shared key. Switch these agents to a model from a provider below." })),
          p.agents ? h("p", { class: "provider-status", text: agentsUsingText(p.agents) }) : null),
        modelsLine(p));
    }

    function modelsLine(p) {
      const models = p.models || [];
      if (!models.length) return null;
      return h("p", { class: "provider-models" }, "Models: ", models.map((m) => m.label).join(", "));
    }

    function providerCard(p) {
      const key = p.key;
      const card = h("section", { class: "card provider-card" });
      const replaceWith = (provider) => {
        storeProvider(provider);
        if (isCurrent(token)) card.replaceWith(providerCard(provider));
      };

      let statusBadge;
      let statusLine;
      if (!key) {
        statusBadge = badge("Not set", "muted");
        statusLine = h("p", { class: "provider-status", text: "No key yet." });
      } else if (!key.usable) {
        statusBadge = badge("Can't be read", "danger");
        statusLine = h("p", { class: "provider-status provider-status-bad" }, icon("alert"), h("span", { text: "Can't be read. Paste it again." }));
      } else {
        statusBadge = badge("Key saved", "ok");
        const checked = key.verified_at || key.updated_at;
        statusLine = h("p", { class: "provider-status" }, icon("check"),
          h("span", null, "Key ", h("code", { class: "key-hint", text: key.hint || "" }),
            checked ? [", checked ", timeEl(checked)] : null));
      }

      const keyInput = h("input", {
        type: "password", autocomplete: "off", spellcheck: "false",
        placeholder: key && key.usable ? `Paste a new key to replace ${key.hint || "the saved one"}` : p.key_placeholder || "API key",
      });
      // key_url comes from the server's catalog; only plain https links become links.
      const getKey = p.key_url && /^https:\/\//i.test(p.key_url)
        ? h("a", { class: "external-link", href: p.key_url, target: "_blank", rel: "noopener noreferrer" }, "Get a key", icon("external"))
        : null;

      let baseUrl = null;
      let modelsInput = null;
      let extraModels = null;
      const savedModels = modelListText(key ? key.models : []);
      if (p.custom_url) {
        baseUrl = h("input", { type: "text", inputmode: "url", autocomplete: "off", spellcheck: "false", placeholder: "https://example.com/v1", value: (key && key.base_url) || "" });
        modelsInput = h("input", { type: "text", autocomplete: "off", spellcheck: "false", placeholder: "e.g. my-model, my-other-model", value: savedModels });
      } else {
        extraModels = h("input", { type: "text", autocomplete: "off", spellcheck: "false", placeholder: "model-id, another-model-id", value: savedModels });
      }

      const error = h("p", { class: "form-error", role: "alert", hidden: true });
      const showErr = (message) => { error.textContent = message; error.hidden = false; };
      const save = h("button", { class: "btn btn-primary", type: "submit" });
      const saveLabel = () => setChildren(save, !key || !key.usable ? "Save key" : keyInput.value.trim() ? "Replace key" : "Save changes");
      saveLabel();
      keyInput.addEventListener("input", saveLabel);
      const test = key && key.usable ? h("button", { class: "btn", type: "button", text: "Test" }) : null;
      const remove = key ? h("button", { class: "btn btn-danger-ghost", type: "button" }, icon("trash"), "Remove") : null;

      const fields = h("fieldset", { class: "provider-form" },
        p.custom_url ? field("Base URL", baseUrl, "The server's OpenAI-compatible API root, usually ending in /v1.") : null,
        p.custom_url ? field("Model ids", modelsInput, "Comma separated: the models this server offers.") : null,
        h("label", { class: "field" },
          h("span", { class: "field-label-row" }, h("span", { class: "field-label", text: "API key" }), getKey),
          keyInput),
        extraModels ? h("details", { class: "extra-models", open: false },
          h("summary", { text: savedModels ? `Extra models (${savedModels.split(", ").length})` : "Extra models" }),
          field("Extra model ids", extraModels, "Comma separated. Models this provider offers that are not in the list above.")) : null,
        error,
        h("div", { class: "provider-actions" }, save, test, remove));

      const form = h("form", { class: "provider-form-wrap", novalidate: true }, fields);
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        error.hidden = true;
        const body = {};
        const typed = keyInput.value.trim();
        if (typed) body.api_key = typed;
        else if (!key) { showErr("Paste the API key."); keyInput.focus(); return; }
        if (p.custom_url) {
          body.base_url = baseUrl.value.trim();
          body.models = modelsInput.value;
          if (!body.base_url) { showErr("Enter the server's base URL."); baseUrl.focus(); return; }
          if (!modelListText(body.models)) { showErr("List at least one model id this server offers."); modelsInput.focus(); return; }
        } else if (modelListText(extraModels.value) !== savedModels) {
          body.models = extraModels.value;
        }
        if (key && !typed && Object.keys(body).length === 0) {
          showErr("Paste a new key, or change the models, then save.");
          return;
        }
        // The server checks the key with the provider before saving; that can take a few seconds.
        fields.disabled = true;
        withBusyLabel(save, "Checking key…", async () => {
          try {
            const res = await api("PUT", `/api/providers/${enc(p.id)}/key`, body);
            toast(`${p.label}: key saved and working.`, "ok");
            replaceWith(res.provider);
          } catch (err) {
            if (err.status === 401) return;
            showErr(err.message);
          } finally {
            fields.disabled = false;
          }
        });
      });

      if (test) {
        test.addEventListener("click", () => {
          error.hidden = true;
          fields.disabled = true;
          withBusyLabel(test, "Testing…", async () => {
            try {
              const res = await api("POST", `/api/providers/${enc(p.id)}/key/test`);
              if (res.ok) {
                toast(`${p.label}: ${res.message || "The key works."}`, "ok");
                replaceWith(res.provider);
              } else {
                showErr(res.message || "The key did not work.");
                storeProvider(res.provider);
              }
            } catch (err) {
              if (err.status !== 401) showErr(err.message);
            } finally {
              fields.disabled = false;
            }
          });
        });
      }

      if (remove) {
        remove.addEventListener("click", () => {
          const warning = p.agents
            ? `Remove your ${p.label} key?\n\n${agentsUsingText(p.agents)} ${p.agents === 1 ? "It stops" : "They stop"} working until you add a key again or switch ${p.agents === 1 ? "it" : "them"} to another model.`
            : `Remove your ${p.label} key?`;
          if (!window.confirm(warning)) return;
          error.hidden = true;
          withBusy(remove, async () => {
            try {
              await api("DELETE", `/api/providers/${enc(p.id)}/key`);
              toast(`${p.label} key removed.`, "ok");
              await reload();
            } catch (err) {
              if (err.status !== 401) showErr(err.message);
            }
          });
        });
      }

      return appendAll(card,
        [h("div", { class: "provider-head" }, h("h2", { text: p.label }), statusBadge),
          p.description ? h("p", { class: "muted", text: p.description }) : null,
          h("div", { class: "provider-facts" }, statusLine,
            p.agents ? h("p", { class: "provider-status", text: agentsUsingText(p.agents) }) : null),
          modelsLine(p),
          form]);
    }
  }

  // ---- usage --------------------------------------------------------------

  const intFormat = new Intl.NumberFormat();

  function fmtInt(n) {
    return intFormat.format(Number(n) || 0);
  }

  function fmtUsd(value) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    if (n === 0) return "$0.00";
    if (n < 0.01) return "< $0.01";
    return `$${n.toFixed(2)}`;
  }

  async function mountUsageTab(body, token) {
    const card = h("section", { class: "card usage-card" }, loadingRow("Loading usage…"));
    body.appendChild(card);
    let res;
    try {
      // Providers only supply model names; the table shows ids without them.
      [res] = await Promise.all([api("GET", withQuery("/api/usage", { days: 30 })), loadProviders().catch(() => null)]);
    } catch (err) {
      if (isCurrent(token)) setChildren(card, errorBlock(err.message));
      return;
    }
    if (!isCurrent(token)) return;
    const rows = (res && res.by_model) || [];
    const days = (res && res.days) || 30;
    const head = h("div", { class: "usage-head" },
      h("h2", { text: `Last ${days} days` }),
      res && res.since ? h("span", { class: "muted small", text: `Since ${fmtDateTime(res.since)}` }) : null);
    if (!rows.length) {
      setChildren(card, head, emptyBlock("No model usage yet. It shows up here once your agents answer messages or run jobs."));
      return;
    }

    const totals = { requests: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 };
    for (const r of rows) for (const k of Object.keys(totals)) totals[k] += Number(r[k]) || 0;
    const anyCost = rows.some((r) => r.estimated_usd !== null && r.estimated_usd !== undefined);
    const num = (text, extra) => h("td", { class: extra ? `num ${extra}` : "num", text });

    const table = h("table", { class: "data-table" },
      h("thead", null, h("tr", null,
        h("th", { scope: "col", text: "Model" }),
        h("th", { scope: "col", text: "Requests" }),
        h("th", { scope: "col", text: "Input" }),
        h("th", { scope: "col", class: "col-cached", text: "Cached", title: "Input tokens read from the provider's prompt cache" }),
        h("th", { scope: "col", text: "Output" }),
        h("th", { scope: "col", text: "Cost", title: "Estimated cost at list prices" }))),
      h("tbody", null, rows.map((r) => h("tr", null,
        h("td", null, h("div", { class: "usage-model" },
          h("span", { class: "usage-model-name", text: modelLabel(r.model, r.provider) }),
          h("span", { class: "usage-provider", text: r.provider_label || r.provider || "" }))),
        num(fmtInt(r.requests)),
        num(fmtInt(r.input_tokens)),
        num(fmtInt(r.cached_input_tokens), "col-cached"),
        num(fmtInt(r.output_tokens)),
        num(fmtUsd(r.estimated_usd))))),
      h("tfoot", null, h("tr", null,
        h("td", { text: "Total" }),
        num(fmtInt(totals.requests)),
        num(fmtInt(totals.input_tokens)),
        num(fmtInt(totals.cached_input_tokens), "col-cached"),
        num(fmtInt(totals.output_tokens)),
        num(fmtUsd(res.estimated_usd)))));

    setChildren(card, head,
      h("div", { class: "table-wrap" }, table),
      h("p", { class: "muted small" },
        anyCost
          ? "Costs are estimates at list prices and are only shown for Claude models; the total covers those models. Your provider's bill is the real amount."
          : "Costs are estimates at list prices and are only shown for Claude models."));
  }

  // ---- account ------------------------------------------------------------

  function mountAccountTab(body) {
    const user = state.user || {};

    // Change password
    const current = h("input", { type: "password", autocomplete: "current-password", required: true });
    const next = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8, maxlength: 200 });
    const again = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8, maxlength: 200 });
    const pwError = h("p", { class: "form-error", role: "alert", hidden: true });
    const pwSave = h("button", { class: "btn btn-primary", type: "submit", text: "Change password" });
    const pwForm = h("form", { class: "stack" },
      // Lets password managers file the new password under the right account.
      h("input", { type: "email", autocomplete: "username", value: user.email || "", hidden: true, readOnly: true, tabindex: "-1" }),
      field("Current password", current),
      field("New password", next, "At least 8 characters."),
      field("New password again", again),
      pwError,
      h("div", { class: "form-actions" }, pwSave));
    pwForm.addEventListener("submit", (e) => {
      e.preventDefault();
      pwError.hidden = true;
      if (next.value !== again.value) { pwError.textContent = "The new passwords do not match."; pwError.hidden = false; return; }
      withBusy(pwSave, async () => {
        try {
          await api("POST", "/api/auth/password", { current_password: current.value, new_password: next.value });
          pwForm.reset();
          toast("Password changed. Your other sessions were signed out.", "ok");
        } catch (err) {
          if (err.status === 401) return;
          pwError.textContent = err.message;
          pwError.hidden = false;
        }
      });
    });

    // Delete account: needs the password and a ticked box.
    const delPassword = h("input", { type: "password", autocomplete: "current-password", required: true });
    const understand = h("input", { type: "checkbox" });
    const delError = h("p", { class: "form-error", role: "alert", hidden: true });
    const delBtn = h("button", { class: "btn btn-danger", type: "submit", disabled: true }, icon("trash"), "Delete my account");
    const syncDelete = () => { delBtn.disabled = !(understand.checked && delPassword.value); };
    understand.addEventListener("change", syncDelete);
    delPassword.addEventListener("input", syncDelete);
    const delForm = h("form", { class: "stack" },
      field("Your password", delPassword),
      h("label", { class: "check-row" }, understand,
        h("span", { text: "I understand that my agents, their files, conversations and jobs, my API keys and my usage history are deleted for good." })),
      delError,
      h("div", { class: "form-actions" }, delBtn));
    delForm.addEventListener("submit", (e) => {
      e.preventDefault();
      delError.hidden = true;
      if (!understand.checked || !delPassword.value) return;
      withBusyLabel(delBtn, "Deleting…", async () => {
        try {
          await api("POST", "/api/auth/delete-account", { password: delPassword.value });
        } catch (err) {
          if (err.status === 401) return;
          delError.textContent = err.message;
          delError.hidden = false;
          return;
        }
        state.user = null;
        state.providers = null;
        closeSession();
        renderUserBox();
        toast("Your account was deleted.", "info");
        go("#/login");
      }).finally(syncDelete);
    });

    body.append(
      h("section", { class: "card account-card" },
        h("h2", { text: "Account" }),
        h("dl", { class: "facts" },
          h("div", null, h("dt", { text: "Email" }), h("dd", { text: user.email || "" })),
          user.timezone ? h("div", null, h("dt", { text: "Timezone" }), h("dd", { text: user.timezone })) : null)),
      h("section", { class: "card account-card" },
        h("h2", { text: "Change password" }),
        h("p", { class: "muted small", text: "Other browsers signed in to this account are signed out. This one stays signed in." }),
        pwForm),
      h("section", { class: "card account-card danger-card" },
        h("h2", { text: "Delete account" }),
        h("p", { class: "muted small", text: "Removes every agent with its container, files, conversation and jobs, plus your API keys and usage. This cannot be undone." }),
        delForm));
  }

  // ======================================================================
  // 13. Boot
  // ======================================================================

  // ---- Light and dark theme ----------------------------------------------
  // index.html applies the theme before the first paint; this wires up the
  // header toggle. A choice made with the toggle is stored and wins. Until the
  // user makes one, the page follows the system setting, even if it changes
  // while the page is open.

  const THEME_KEY = "agentwaiter-theme";
  const systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  /** The theme picked with the toggle, or null if the user never picked one. */
  function storedTheme() {
    try {
      const theme = localStorage.getItem(THEME_KEY);
      return theme === "light" || theme === "dark" ? theme : null;
    } catch (err) {
      return null; // storage blocked: follow the system
    }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const btn = document.getElementById("theme-toggle");
    const label = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    btn.setAttribute("aria-label", label);
    btn.title = label;
    btn.replaceChildren(icon(theme === "dark" ? "sun" : "moon"));
  }

  function initTheme() {
    // Also kept in memory, so a choice still holds against system changes
    // when storage is blocked and setItem below fails.
    let chosen = storedTheme();
    applyTheme(chosen || (systemDark && systemDark.matches ? "dark" : "light"));
    document.getElementById("theme-toggle").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      chosen = next;
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (err) {
        // Storage blocked: the choice holds (in memory) until the page reloads.
      }
      applyTheme(next);
    });
    if (systemDark) {
      const follow = (e) => { if (!chosen) applyTheme(e.matches ? "dark" : "light"); };
      if (systemDark.addEventListener) systemDark.addEventListener("change", follow);
      else if (systemDark.addListener) systemDark.addListener(follow); // older Safari
    }
  }

  async function boot() {
    initTheme();
    document.getElementById("logout-btn").addEventListener("click", logout);
    document.getElementById("settings-link").prepend(icon("gear"));
    window.addEventListener("hashchange", route);
    const root = document.getElementById("app");
    setChildren(root, loadingRow("Loading…"));
    try {
      state.config = await api("GET", "/api/config", undefined, { allow401: true });
    } catch (err) {
      console.error("Could not load /api/config", err);
      setChildren(root, h("div", { class: "card narrow center" },
        h("h1", { text: "AgentWaiter is not reachable" }),
        h("p", { class: "muted", text: err.message }),
        h("button", { class: "btn btn-primary", type: "button", text: "Try again", onclick: () => window.location.reload() })));
      return;
    }
    document.getElementById("dev-banner").hidden = state.config.runtime !== "local";
    try {
      const me = await api("GET", "/api/auth/me", undefined, { allow401: true });
      state.user = me.user;
    } catch (err) {
      if (err.status !== 401) showError(err);
      state.user = null;
    }
    renderUserBox();
    if (!location.hash) location.replace(state.user ? "#/agents" : "#/login");
    else route();
  }

  boot();
})();
