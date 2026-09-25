"use strict";

const GLOW_MS = 30000;
const SLIDE_MS = 1000;
const TICK_MS = 1000;
const RETRY_MS = 5000;
const THEME_KEY = "freebuff-search.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

const state = {
  board: null,
  rows: new Map(),
  pushedAt: 0,
  theme: "system",
};

const $ = (id) => document.getElementById(id);

function element(name, className, text) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function elapsedText(ms) {
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  const total = Math.round(seconds);
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${pad(total % 60)}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${pad(minutes % 60)}m`;
}

function clockText(ms) {
  const date = new Date(ms);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:` +
         `${pad(date.getSeconds())}`;
}

function countText(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `${response.status} ${response.statusText}`);
  }
  return body;
}

function whoText(role) {
  if (role === "assistant") return "agent";
  return role || "";
}

function modelText(model) {
  return String(model || "").split("/").pop();
}

function threadHref(thread) {
  const params = new URLSearchParams();
  params.set("project", thread.project_key || "");
  params.set("thread", thread.id || "");
  params.set("scope", "thread");
  return `/#${params.toString()}`;
}

function rowNodes(thread) {
  const row = element("a", "row running");
  const cell = element("div", "cell");
  const title = element("div", "title");
  const last = element("div", "last");
  const who = element("span", "who");
  const said = element("span");
  last.append(who, said);
  cell.append(title, last);

  const tail = element("div", "tail");
  const runtime = element("span", "runtime");
  const dot = element("span", "dot");
  const runtimeText = element("span", "clock");
  const sep = element("span", "sep", "·");
  const model = element("span", "model");
  runtime.append(dot, runtimeText, sep, model);
  const meta = element("span", "rmeta");
  const age = element("span", "rmeta");
  tail.append(runtime, meta, age);

  row.append(cell, tail);
  return { node: row, title, last, who, said, runtime, runtimeText, dot, sep,
           model, meta, age };
}

function paintRow(entry) {
  const thread = entry.thread;
  const nodes = entry.nodes;
  const who = whoText(thread.last_role);
  nodes.node.classList.toggle("from-agent", who === "agent");
  nodes.node.classList.toggle("from-user", who === "user");
  nodes.node.href = threadHref(thread);
  nodes.title.textContent = thread.title || "(untitled)";
  nodes.title.title = thread.title || "(untitled)";
  nodes.who.textContent = who;
  nodes.who.hidden = !who;
  nodes.said.textContent = thread.last_text || "";
  nodes.last.hidden = !thread.last_text;
  const named = thread.title || "(untitled)";
  nodes.meta.textContent = thread.project || "";
  nodes.meta.title = thread.project || "";
  nodes.meta.hidden = !thread.project;
  nodes.model.textContent = thread.model ? modelText(thread.model) : "";
  nodes.model.title = thread.model || "";
  nodes.model.hidden = !thread.model;
  nodes.sep.hidden = !thread.model;
}

function tick() {
  const board = state.board;
  if (!board) return;
  const now = Date.now();
  let running = 0;
  for (const entry of state.rows.values()) {
    const thread = entry.thread;
    const done = entry.ended;
    const span = done ? Math.max(0, entry.endedAt - thread.started_at)
                      : Math.max(0, now - thread.started_at);
    if (!done) running += 1;
    entry.nodes.runtimeText.textContent =
      (done ? "Ran " : "Running ") + elapsedText(span);
    entry.nodes.age.textContent =
      `${countText(thread.messages, "message", "messages")} · last msg ` +
      `${elapsedText(now - thread.last_ts)} ago`;
  }
  $("count").textContent = String(running);
  $("cap").textContent = running === 1 ? "Thread running" : "Threads running";
  paintStamp();
}

function paintStamp() {
  if (!state.board) return;
  const beat = state.pushedAt ? clockText(state.pushedAt) : "—";
  const stream = state.board.beat > 0 ? `beat ${state.board.beat} s`
                                      : "no change stream";
  $("stamp").textContent = `${location.host} · ${stream} · updated ${beat}`;
}

function clearTimers(entry) {
  for (const id of entry.timers) window.clearTimeout(id);
  entry.timers = [];
}

/* How long a finished turn is shown for is the server's to say: the board
   carries `close_ms`, read from the config's activity.close_seconds, on every
   answer and every push. The 30 s constant stands in only when the board
   carried none — 0 is a value, and slides the row off the moment it ends. */
function closeMs() {
  const served = state.board ? Number(state.board.close_ms) : NaN;
  return Number.isFinite(served) && served >= 0 ? served : GLOW_MS;
}

function glowOut(id, entry) {
  entry.timers.push(window.setTimeout(() => {
    entry.nodes.node.classList.add("away");
    entry.timers.push(window.setTimeout(() => {
      entry.nodes.node.remove();
      state.rows.delete(id);
    }, SLIDE_MS));
  }, closeMs()));
}

function laneFor(entry) {
  return entry.thread.last_role === "user" ? "from-user" : "from-agent";
}

function finish(entry, when) {
  entry.ended = true;
  entry.endedAt = when || Date.now();
  const node = entry.nodes.node;
  node.classList.remove("running", "from-user", "from-agent");
  node.classList.add("done", laneFor(entry));
  entry.nodes.dot.hidden = true;
  glowOut(entry.thread.id, entry);
}

/* A turn that ends and starts again inside its 30 s glow is running once
   more, so the row is put back rather than left looking finished: the exit
   timers are called off, the row loses `done` and any slide under way, and
   its dot comes back. */
function revive(entry, thread) {
  clearTimers(entry);
  entry.thread = thread;
  entry.ended = false;
  entry.endedAt = 0;
  const node = entry.nodes.node;
  node.classList.remove("done", "away");
  node.classList.add("running");
  entry.nodes.dot.hidden = false;
  paintRow(entry);
}

function paintAll(board) {
  state.board = board;
  state.pushedAt = Date.now();
  const list = $("list");
  const seen = new Set();
  for (const thread of board.threads) {
    seen.add(thread.id);
    const found = state.rows.get(thread.id);
    if (!found) {
      /* a turn that had already ended when this page looked is not a row:
         the board carries it for the glow of whoever was watching it happen */
      if (thread.finished) continue;
      const made = { nodes: rowNodes(thread), thread, ended: false, endedAt: 0,
                     timers: [] };
      paintRow(made);
      state.rows.set(thread.id, made);
      list.append(made.nodes.node);
      continue;
    }
    found.thread = thread;
    paintRow(found);
    if (found.ended) {
      if (!thread.finished) revive(found, thread);
    } else if (thread.finished) {
      finish(found, thread.finished_at);
    } else {
      list.append(found.nodes.node);
    }
  }
  for (const [id, entry] of state.rows) {
    if (!seen.has(id) && !entry.ended) finish(entry);
  }
  tick();
}

function paintTheme() {
  for (const choice of ["light", "dark", "system"]) {
    $(`theme-${choice}`).classList.toggle("on", state.theme === choice);
  }
}

function systemTheme() {
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

function applyTheme() {
  document.documentElement.setAttribute(
    "data-theme", state.theme === "system" ? systemTheme() : state.theme);
}

function followSystemTheme() {
  window.matchMedia(DARK_QUERY).addEventListener("change", () => {
    if (state.theme === "system") applyTheme();
  });
}

function chooseTheme(theme) {
  state.theme = theme;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (error) {

  }
  applyTheme();
  paintTheme();
}

function wireTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") {
      state.theme = stored;
    }
  } catch (error) {

  }
  applyTheme();
  followSystemTheme();
  paintTheme();
  $("gear").addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = $("menu");
    menu.hidden = !menu.hidden;
    $("gear").setAttribute("aria-expanded", menu.hidden ? "false" : "true");
  });
  for (const choice of ["light", "dark", "system"]) {
    $(`theme-${choice}`).addEventListener("click", () => chooseTheme(choice));
  }
  document.addEventListener("click", () => {
    $("menu").hidden = true;
    $("gear").setAttribute("aria-expanded", "false");
  });
}

let stream = null;
let retry = null;

/* The stream is opened by the look that succeeds, never by the first one: a
   board that could not be read once must not leave the page watching nothing
   until the reader reloads, so a failed load retries and the stream follows
   the answer it was waiting for. */
function watchStream() {
  if (stream || !window.EventSource || !state.board || state.board.beat <= 0) return;
  stream = new EventSource("/api/events");
  stream.onmessage = (message) => {
    let payload = {};
    try {
      payload = JSON.parse(message.data);
    } catch (error) {
      return;
    }
    if (payload.board) paintAll(payload.board);
  };
  stream.onopen = () => {
    load();
  };
}

async function load() {
  try {
    paintAll(await api("/api/running"));
    if (retry !== null) {
      window.clearTimeout(retry);
      retry = null;
    }
    watchStream();
  } catch (error) {
    $("stamp").textContent = `the server did not answer /api/running: ${error}`;
    if (retry === null) retry = window.setTimeout(load, RETRY_MS);
  }
}

async function start() {
  wireTheme();
  await load();
  window.setInterval(tick, TICK_MS);
}

start();
