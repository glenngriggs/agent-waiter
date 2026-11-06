/*
 * Den web client: plain JavaScript, no framework, no build step, no CDN.
 *
 * It talks only to the Den backend (docs/designs/agent-platform.md, sections 4.1 and 4.3).
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
 *   5. Auth, agent list, onboarding 12. Boot
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
    sparkle: "M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18",
    refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
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
    // A stable colour per agent so cards are easy to tell apart.
    let hash = 0;
    for (const ch of String(id || name)) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    const initial = (String(name || "?").trim()[0] || "?").toUpperCase();
    return h("span", { class: "avatar", style: { backgroundColor: `hsl(${hash % 360} 55% 46%)` }, text: initial, "aria-hidden": "true" });
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
      throw new ApiError("Could not reach the Den server. Check that it is running, then try again.", 0, null);
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
    closeSession();
    renderUserBox();
    const page = parseHash().parts[0];
    if (page !== "login" && page !== "register") redirect("#/login");
  }

  function signedIn(user) {
    state.user = user;
    renderUserBox();
    go("#/agents");
  }

  function modelLabel(modelId) {
    const m = ((state.config && state.config.models) || []).find((x) => x.id === modelId);
    return m ? m.label : String(modelId || "");
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
    document.title = "Den";

    let view;
    if (page === "login") view = viewLogin(root);
    else if (page === "register") view = viewRegister(root);
    else if (page === "agents") view = viewAgents(root, token);
    else if (page === "new") view = viewNewAgent(root, token);
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
      h("p", { class: "muted", text: "That address does not match anything in Den." }),
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
    document.title = "Sign in · Den";
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
    root.appendChild(authShell("Welcome back", "Sign in to talk to your agents.", form,
      h("p", { class: "auth-switch" }, "New to Den? ", h("a", { href: "#/register", text: "Create an account" }))));
    email.focus();
  }

  function viewRegister(root) {
    document.title = "Create account · Den";
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
    root.appendChild(authShell("Create your account", "Then set up your first agent in under a minute.", form,
      h("p", { class: "auth-switch" }, "Already have an account? ", h("a", { href: "#/login", text: "Sign in" }))));
    email.focus();
  }

  // ---- agents list ------------------------------------------------------

  async function viewAgents(root, token) {
    document.title = "Agents · Den";
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
        res = await api("GET", "/api/agents");
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
          h("p", { class: "muted", text: "Create an agent, give it a name and a model, and start chatting. It keeps working when you close the tab." }),
          h("a", { class: "btn btn-primary", href: "#/new" }, icon("plus"), "Create your first agent")));
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
          h("span", { class: "muted small", text: modelLabel(a.model) }))),
      h("div", { class: "badges" },
        setup,
        a.setup_state === "ready" ? containerBadge(a.container) : null,
        a.chat_busy ? badge("Working", "info", true) : null),
      h("div", { class: "muted small" }, "Last active ", timeEl(a.last_active_at)));
  }

  // ---- model picker (onboarding, retry) ----------------------------------

  function modelPicker(selected, groupName) {
    const models = (state.config && state.config.models) || [];
    const radios = [];
    const el = h("div", { class: "model-picker", role: "radiogroup" }, models.map((m, i) => {
      const radio = h("input", { type: "radio", name: groupName, value: m.id, checked: selected ? m.id === selected : i === 0 });
      radios.push(radio);
      return h("label", { class: "model-option" }, radio,
        h("span", { class: "model-option-body" },
          h("span", { class: "model-option-label", text: m.label }),
          h("span", { class: "model-option-id", text: m.id })));
    }));
    return {
      el,
      value: () => {
        const r = radios.find((x) => x.checked);
        return r ? r.value : null;
      },
    };
  }

  // ---- new agent ----------------------------------------------------------

  function viewNewAgent(root, token) {
    document.title = "New agent · Den";
    const name = h("input", { type: "text", maxlength: 40, required: true, placeholder: "e.g. Research helper", autocomplete: "off" });
    const picker = modelPicker(null, "new-agent-model");
    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Create agent");
    const card = h("div", { class: "card narrow" });
    const form = h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        error.hidden = true;
        const trimmed = name.value.trim();
        if (!trimmed) { error.textContent = "Give your agent a name."; error.hidden = false; return; }
        withBusy(submit, async () => {
          let res;
          try {
            res = await api("POST", "/api/agents", { name: trimmed, model: picker.value() });
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
      h("span", { class: "field-hint", text: "The model it thinks with. You can switch later." })),
    error,
    h("div", { class: "form-actions" }, h("a", { class: "btn btn-ghost", href: "#/agents", text: "Cancel" }), submit));

    card.append(
      h("h1", { text: "Create an agent" }),
      h("p", { class: "muted", text: "Your agent gets its own container, a workspace for files, and a model to think with." }),
      form);
    root.appendChild(card);
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
      const picker = modelPicker(session.agent && session.agent.model, `retry-model-${session.agentId}`);
      const retry = h("button", { class: "btn btn-primary", type: "button" }, icon("restart"), "Retry setup");
      retry.addEventListener("click", () => withBusy(retry, async () => {
        try {
          const chosen = picker.value();
          if (chosen && session.agent && chosen !== session.agent.model) {
            const patched = await api("PATCH", agentUrl(session.agentId), { model: chosen });
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
        h("div", { class: "field" }, h("span", { class: "field-label", text: "Try again with model" }), picker.el),
        h("div", { class: "form-actions" }, retry));
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
      this.draft = "";
      this.expandedJobs = new Set();
      this.timeline = new Timeline({
        agentId,
        mode: "chat",
        emptyText: "No messages yet. Say hello, or ask the agent to do something in its workspace.",
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
        case "turn_started":
          if (ev.channel === "chat") this.setBusy(true, ev.turn_id);
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

  // Short headings for the red failed-turn card; the server's error_message is the body.
  const ERROR_TITLES = {
    unauthorized: "Credentials rejected",
    rate_limited: "Rate limited",
    model_unavailable: "Model unavailable",
    context_full: "Conversation full",
    runtime_crashed: "Runtime crashed",
    container_failed: "Container failed",
    timeout: "Timed out",
    platform_restart: "Platform restarted",
    other: "Turn failed",
  };

  /** Render agent-message text: plain text, with ``` fenced blocks and `inline code`. */
  function renderRichText(el, text) {
    el.replaceChildren();
    const fence = /```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g;
    let last = 0;
    let match;
    while ((match = fence.exec(text)) !== null) {
      if (match.index > last) appendInline(el, text.slice(last, match.index).replace(/\n$/, ""));
      const pre = h("pre", { class: "code-block" }, h("code", { text: match[2].replace(/\n$/, "") }));
      const lang = match[1].trim();
      if (lang) pre.dataset.lang = lang;
      el.appendChild(pre);
      last = fence.lastIndex;
      if (match[0].length === 0) fence.lastIndex += 1; // never loop on an empty match
      if (last >= text.length) break;
    }
    if (last < text.length) appendInline(el, text.slice(last).replace(/^\n/, ""));
  }

  function appendInline(el, text) {
    if (text) el.appendChild(richInline(text));
  }

  /** A span with `backticked` runs shown as inline code; everything else is plain text. */
  function richInline(text) {
    const span = h("span", { class: "rich-text" });
    String(text).split(/(`[^`\n]+`)/).forEach((part, i) => {
      if (i % 2 === 1) span.appendChild(h("code", { class: "inline-code", text: part.slice(1, -1) }));
      else if (part) span.appendChild(document.createTextNode(part));
    });
    return span;
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

  class ReasoningEntry {
    constructor() {
      this.done = false;
      this.el = h("div", { class: "thinking" }, h("span", { class: "typing" }, h("i"), h("i"), h("i")), h("span", { text: "Thinking…" }));
    }
    update(d, completed) {
      if (!completed) return;
      this.done = true;
      const text = typeof d.text === "string" ? d.text.trim() : "";
      if (!text) {
        // Nothing to show once the thought is over; keep the timeline quiet.
        this.el.hidden = true;
        return;
      }
      const details = h("details", { class: "thinking thinking-done" },
        h("summary", null, icon("sparkle"), "Thought"),
        h("p", { class: "thinking-text", text }));
      this.el.replaceWith(details);
      this.el = details;
    }
    finish() {
      this.done = true;
      this.el.hidden = true;
    }
  }

  class CommandEntry {
    constructor(tl) {
      this.tl = tl;
      this.output = "";
      this.done = false;
      this.failed = false;
      this.userToggled = false;
      this.pending = false;
      this.cmdEl = h("code", { class: "tool-cmd" });
      this.badges = h("span", { class: "tool-badges" });
      this.outEl = h("pre", { class: "tool-output", hidden: true });
      const summary = h("summary", { class: "tool-summary", onclick: () => { this.userToggled = true; } },
        icon("terminal", "tool-kind"), this.cmdEl, this.badges);
      this.el = h("details", { class: "tool tool-command is-running" }, summary, this.outEl);
    }
    update(d, completed) {
      const cmd = Array.isArray(d.command) ? d.command.join(" ") : String(d.command || "");
      if (cmd) {
        this.cmdEl.textContent = cmd;
        this.cmdEl.title = cmd;
      }
      // The completed item carries the authoritative output tail; keep live output otherwise.
      if (typeof d.output === "string" && d.output && (completed || d.output.length > this.output.length)) this.output = d.output;
      const status = d.status || (completed ? "completed" : "inProgress");
      this.done = completed || status !== "inProgress";
      const exit = d.exit_code;
      const hasExit = exit !== null && exit !== undefined;
      this.failed = (hasExit && exit !== 0) || status === "failed" || status === "declined";
      const parts = [];
      if (!this.done) parts.push(spinner());
      // Non-zero exit codes are red: a turn can "complete" with every command failed.
      if (hasExit) parts.push(badge(`exit ${exit}`, exit === 0 ? "ok" : "danger"));
      else if (status === "failed" || status === "declined") parts.push(badge(status, "danger"));
      if (typeof d.duration_ms === "number") parts.push(h("span", { class: "tool-dur", text: fmtDuration(d.duration_ms) }));
      setChildren(this.badges, ...parts);
      this.el.classList.toggle("is-running", !this.done);
      this.el.classList.toggle("is-failed", this.failed);
      this.renderOutput();
      // Open while running (live output) and when it failed; collapse successes.
      if (!this.userToggled) this.el.open = !this.done || this.failed;
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
    finish(reason) {
      if (this.done) return;
      this.done = true;
      this.el.classList.remove("is-running");
      setChildren(this.badges, badge(reason === "unfinished" ? "did not finish" : "stopped", "warn"));
      this.renderOutput();
    }
  }

  class FileChangeEntry {
    constructor(tl) {
      this.tl = tl;
      this.done = false;
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
      setChildren(this.list, ...changes.map((c) => this.changeRow(c || {})));
      const parts = [];
      if (!this.done) parts.push(spinner());
      if (status === "failed" || status === "declined") parts.push(badge(status, "danger"));
      setChildren(this.badges, ...parts);
    }
    changeRow(change) {
      // `kind` may be a plain string or the SDK's {type: "add"|"delete"|"update"} object.
      const raw = change.kind && typeof change.kind === "object" ? change.kind.type : change.kind;
      const kind = { add: "added", delete: "deleted", update: "edited" }[raw] || raw || "changed";
      const tone = kind === "added" ? "ok" : kind === "deleted" ? "danger" : "info";
      const rel = workspaceRelative(change.path);
      let target;
      if (rel === null) target = h("span", { class: "mono", text: String(change.path || "") });
      else if (kind === "deleted") target = h("a", { class: "mono", href: filesHash(this.tl.agentId, dirname(rel)), text: rel || "/" });
      else target = h("a", { class: "mono", href: filesHash(this.tl.agentId, dirname(rel), rel), text: rel || "/" });
      return h("li", null, badge(kind, tone), target);
    }
    finish() {
      this.done = true;
      this.badges.replaceChildren();
    }
  }

  class McpEntry {
    constructor() {
      this.done = false;
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
      this.el.classList.toggle("is-failed", failed);
      const parts = [];
      if (!this.done) parts.push(spinner());
      if (failed) parts.push(badge("failed", "danger"));
      setChildren(this.badges, ...parts);
      const result = typeof d.result_text === "string" ? d.result_text : "";
      const first = (err || result).split("\n")[0];
      this.preview.textContent = first.length > 160 ? first.slice(0, 157) + "…" : first;
      setChildren(this.body, 
        d.arguments ? h("div", { class: "tool-section" }, h("div", { class: "tool-section-label", text: "Arguments" }), h("pre", { class: "tool-output", text: prettyJson(d.arguments) })) : null,
        result ? h("div", { class: "tool-section" }, h("div", { class: "tool-section-label", text: "Result" }), h("pre", { class: "tool-output", text: result })) : null,
        err ? h("div", { class: "tool-section" }, h("div", { class: "tool-section-label", text: "Error" }), h("pre", { class: "tool-output tool-error", text: err })) : null);
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
      this.label = h("span");
      this.badges = h("span", { class: "tool-badges" });
      this.el = h("div", { class: "tool-line" }, icon(kind === "web_search" ? "search" : "sparkle", "tool-kind"), this.label, this.badges);
    }
    update(d, completed) {
      this.done = completed;
      if (this.kind === "web_search") this.label.textContent = d.query ? `Searched the web: ${d.query}` : "Searched the web";
      else this.label.textContent = `Agent step: ${d.raw_type || d.item_type || "unknown"}`;
      setChildren(this.badges, completed ? null : spinner());
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
      this.workingEl = h("div", { class: "working" }, h("span", { class: "typing" }, h("i"), h("i"), h("i")), this.workingLabel);
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
        t = { started: false, items: new Set(), hasMessage: false, failedMessage: null, errorNotices: [] };
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
        this.items.set(key, entry);
        const t = this.turn(ev.turn_id);
        t.items.add(key);
        if (entry instanceof MessageEntry) t.hasMessage = true;
        this.add(entry.el);
      }
      entry.update(d, completed);
    }

    messageEntry(turnId, itemId) {
      const key = `${turnId}:${itemId}`;
      let entry = this.items.get(key);
      if (!entry) {
        entry = new MessageEntry(this);
        this.items.set(key, entry);
        const t = this.turn(turnId);
        t.items.add(key);
        t.hasMessage = true;
        this.add(entry.el);
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
        this.items.set(key, entry);
        this.turn(turnId).items.add(key);
        this.add(entry.el);
        entry.update({ status: "inProgress" }, false);
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
      let el;
      if (level === "info") el = h("div", { class: "divider" }, h("span", { text: message }));
      else el = h("div", { class: `notice notice-${level}`, role: level === "error" ? "alert" : null }, icon("alert"), h("span", { text: message }));
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
        const message = d.error_kind === "interrupted" || !d.error_message ? "Stopped." : d.error_message;
        this.add(h("div", { class: "divider divider-warn" }, h("span", { text: message })));
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
      if ((kind === "model_unavailable" || kind === "rate_limited") && this.actions.switchModel) {
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
        this.add(h("div", { class: "notice notice-error" }, icon("alert"), h("span", { text: d.message || "Agent setup failed." })));
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
    }

    /** Called when the server says nothing is running: no item can still be in progress. */
    finishDangling() {
      for (const entry of this.items.values()) {
        if (!entry.done) entry.finish("unfinished");
      }
    }
  }

  // ======================================================================
  // 8. Agent page: header, settings dialog, tabs
  // ======================================================================

  async function viewAgent(root, token, agentId, tab, query) {
    const session = ensureSession(agentId);
    root.appendChild(loadingRow("Opening agent…"));
    try {
      await session.ready;
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
    document.title = `${agent.name} · Den`;
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
        h("span", { class: "meta-item", text: modelLabel(a.model) }),
        a.setup_state === "ready" ? containerBadge(a.container) : null,
        session.busy ? badge("Working", "info", true) : null,
        connIndicator(session.connState));
      const buttons = [];
      if (a.setup_state === "ready") {
        if (a.container === "running") buttons.push(actionButton("stop", "Stop", "stop", "btn-ghost"));
        else buttons.push(actionButton("start", "Start", "play", "btn-ghost"));
        buttons.push(actionButton("restart", "Restart", "restart", "btn-ghost"));
      }
      const settingsBtn = h("button", { class: "btn btn-sm", type: "button", onclick: () => openSettings(session) }, icon("settings"), "Settings");
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

  function openSettings(session, opts) {
    const a = session.agent;
    if (!a) return;
    const models = ((state.config && state.config.models) || []).slice();
    if (!models.some((m) => m.id === a.model)) models.unshift({ id: a.model, label: `${a.model} (no longer offered)` });
    const name = h("input", { type: "text", value: a.name, maxlength: 40, required: true });
    const model = h("select", null, models.map((m) => h("option", { value: m.id, text: m.label, selected: m.id === a.model })));
    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save" });
    const cancel = h("button", { class: "btn btn-ghost", type: "button", text: "Cancel" });
    const del = h("button", { class: "btn btn-danger-ghost", type: "button" }, icon("trash"), "Delete agent");

    const form = h("form", { class: "stack" },
      h("div", { class: "dialog-head" }, h("h2", { text: "Agent settings" })),
      field("Name", name),
      field("Model", model, "A new model applies from the next message."),
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
      if (model.value !== a.model) body.model = model.value;
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
    const sendBtn = h("button", { class: "btn btn-primary composer-send", type: "submit" }, icon("send"), h("span", { text: "Send" }));
    const stopLabel = h("span", { text: "Stop" });
    const stopBtn = h("button", { class: "btn btn-danger composer-stop", type: "button" }, icon("stop"), stopLabel);
    const form = h("form", { class: "composer" }, input, h("div", { class: "composer-actions" }, sendBtn, stopBtn));
    const hint = h("div", { class: "composer-hint", text: "Enter to send · Shift+Enter for a new line" });
    const panel = h("div", { class: "chat-panel" },
      h("div", { class: "chat-toolbar" }, convLabel, newConvBtn),
      session.timeline.root,
      form,
      hint);
    body.appendChild(panel);

    let sending = false;
    let wasBusy = session.busy;

    const update = () => {
      if (!session.agent) return;
      convLabel.textContent = `Conversation ${session.agent.conversation_no || 1}`;
      const busy = session.busy;
      // Hand the keyboard back when the agent finishes, so the user can reply straight away.
      if (wasBusy && !busy) requestAnimationFrame(() => input.focus());
      wasBusy = busy;
      sendBtn.hidden = busy;
      stopBtn.hidden = !busy;
      stopBtn.disabled = session.stopping;
      stopLabel.textContent = session.stopping ? "Stopping…" : "Stop";
      input.disabled = busy || sending;
      sendBtn.disabled = sending;
      newConvBtn.disabled = busy;
      input.placeholder = busy ? "The agent is working. Stop it or wait for it to finish." : `Message ${session.agent.name}…`;
    };

    const autosize = () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
    };

    async function send() {
      const text = input.value;
      if (!text.trim() || session.busy || sending) return;
      sending = true;
      update();
      try {
        const res = await api("POST", agentUrl(session.agentId, "/messages"), { text });
        session.draft = "";
        input.value = "";
        autosize();
        session.setBusy(true, res.turn_id);
        session.timeline.addUserMessage(res.turn_id, text);
      } catch (err) {
        showError(err);
      } finally {
        sending = false;
        update();
        if (!session.busy) input.focus();
      }
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

    form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
    stopBtn.addEventListener("click", stop);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
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
      else if (e.type === "symlink") toast("This is a link the agent created. Den never follows links, so it cannot be opened here. You can delete it.", "info");
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
        r.error_message ? errorBlock(r.error_message) : null);
    }

    load();
  }

  // ======================================================================
  // 12. Boot
  // ======================================================================

  async function boot() {
    document.getElementById("logout-btn").addEventListener("click", logout);
    window.addEventListener("hashchange", route);
    const root = document.getElementById("app");
    setChildren(root, loadingRow("Loading Den…"));
    try {
      state.config = await api("GET", "/api/config", undefined, { allow401: true });
    } catch (err) {
      console.error("Could not load /api/config", err);
      setChildren(root, h("div", { class: "card narrow center" },
        h("h1", { text: "Den is not reachable" }),
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
