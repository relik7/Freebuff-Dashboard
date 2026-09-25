"use strict";

const LIVE_SEARCH_MS = 50;

const state = {
  projects: [],
  query: "",
  mode: "words",
  scope: "all",
  categories: ["user", "assistant"],
  since: "",
  until: "",
  hideClosed: false,
  archived: false,
  view: null,
  folded: new Set(),
  project: null,
  thread: null,
  status: null,
  schemaLimited: false,
  schemaDetails: [],

  theme: "system",
  debug: false,
};

const $ = (id) => document.getElementById(id);

function element(name, className, text) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

async function api(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, value);
    }
  }
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `${response.status} ${response.statusText}`);
  }
  return body;
}

/* Two routes are the same route when they carry the same parameters, whatever
   the browser did to the encoding on the way round.  Comparing the written
   string with the location would file a second entry for a page already on
   screen, because the target is a fragment and the location is a path. */
function routeKey(params) {
  return [...params.entries()]
    .map(([name, value]) => `${name}=${value}`).sort().join("&");
}

function readRoute() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  state.query = params.get("q") || "";
  state.mode = params.get("mode") || "words";
  state.scope = params.get("scope") || "all";
  const categories = params.get("category");
  if (categories !== null) {
    state.categories = categories ? categories.split(",") : [];
  }
  state.since = params.get("since") || "";
  state.until = params.get("until") || "";
  state.hideClosed = params.get("hide_closed") === "1";
  state.view = params.get("view") || null;
  state.project = params.get("project") || null;
  state.thread = params.get("thread") || null;
}

function writeRoute(push) {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.mode !== "words") params.set("mode", state.mode);
  if (state.scope !== "all") params.set("scope", state.scope);
  const categories = state.categories.join(",");
  if (categories !== DEFAULT_CATEGORIES.join(",")) {
    params.set("category", categories);
  }
  if (state.since) params.set("since", state.since);
  if (state.until) params.set("until", state.until);
  if (state.hideClosed) params.set("hide_closed", "1");
  if (state.view) params.set("view", state.view);
  if (state.project) params.set("project", state.project);
  if (state.thread) params.set("thread", state.thread);
  const hash = params.toString();
  const url = hash ? `#${hash}` : location.pathname + location.search;
  const filed = new URLSearchParams(location.hash.replace(/^#/, ""));
  if (push === true && routeKey(params) !== routeKey(filed)) {
    history.pushState(null, "", url);
  } else history.replaceState(null, "", url);
}

function liveQuery() {
  const box = $("q");
  return box ? box.value : state.query;
}

function commitQuery(push) {
  const text = liveQuery();
  if (text === state.query) return false;
  state.query = text;
  writeRoute(push);
  return true;
}

function refineSearch() {
  commitQuery(false);
  runSearch();
}

function commitSearch() {
  commitQuery(true);
  if (live !== null) {
    clearTimeout(live);
    live = null;
  }
  runSearch();
}

const CATEGORIES = ["user", "assistant", "reasoning", "tools", "changes"];

const DEFAULT_CATEGORIES = ["user", "assistant"];

const CATEGORY_LABELS = {
  user: "user messages",
  assistant: "agent responses",
  reasoning: "thinking",
  tools: "tool runs",
  changes: "file diffs",
};

function applyControls(syncQuery = true) {
  if (syncQuery) $("q").value = state.query;
  $("mode").value = state.mode;
  $("scope").value = state.scope;
  $("since").value = state.since;
  $("until").value = state.until;
  $("hide-closed").checked = state.hideClosed;
  for (const name of CATEGORIES) {
    $(`cat-${name}`).checked = state.categories.includes(name);
  }
  $("debug").checked = state.debug;
  for (const choice of ["light", "dark", "system"]) {
    $(`theme-${choice}`).classList.toggle("on", state.theme === choice);
  }
  paintOptions();
}

const OPTIONS = ["cat-user", "cat-assistant", "cat-reasoning", "cat-tools",
                 "cat-changes", "hide-closed"];

function paintOptions() {
  for (const id of OPTIONS) {
    const box = $(id);
    const chip = box.closest("label");
    if (chip) {
      chip.classList.toggle("on", box.checked);
      chip.classList.toggle("off", !box.checked);
    }
  }
  for (const field of ["since", "until"]) {
    const chip = $(field).closest("label");
    if (chip) chip.classList.add("off");
  }
}

const THEME_KEY = "freebuff-search.theme";
const DEBUG_KEY = "freebuff-search.debug";
const FOLDED_KEY = "freebuff-search.folded";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function loadPreferences() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    if (theme === "light" || theme === "dark" || theme === "system") {
      state.theme = theme;
    }
    state.debug = localStorage.getItem(DEBUG_KEY) === "1";
    const folded = JSON.parse(localStorage.getItem(FOLDED_KEY) || "[]");
    if (Array.isArray(folded)) {
      state.folded = new Set(folded.filter((one) => typeof one === "string"));
    }
  } catch (error) {

  }
  applyTheme();
  followSystemTheme();
}

function savePreference(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (error) {

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

function isFolded(project) {
  return state.folded.has(project.path);
}

/* Each project is its own row, with the threads it holds in a block right after
   it, and a fold is that block's own `hidden` rather than a rebuilt list. The
   row is deliberately outside the block: it carries the fold, so hiding the
   block must leave the row the reader clicks to open it again. A fold changes
   one project, so rebuilding the tree for it threw away everything else the
   reader had: the place they were reading, because a rebuilt tree is a tree
   whose scroll anchor was deleted and whose offset the engine is then free to
   re-clamp; the focus the click had just put on the chevron, which a replaced
   node cannot keep; and the chevron's own quarter turn, which a replaced node
   cannot animate. */
function paintFold(project) {
  const folded = isFolded(project);
  const found = treeBlocks.get(project.key);
  if (!found) return;
  found.block.hidden = folded;
  found.row.classList.toggle("folded", folded);
  const button = found.row.querySelector(".chev");
  button.setAttribute("aria-expanded", folded ? "false" : "true");
  button.setAttribute("aria-label",
    `${folded ? "Show" : "Hide"} ${project.label}'s threads`);
}

function setFold(project, folded) {
  if (folded) state.folded.add(project.path);
  else state.folded.delete(project.path);
  savePreference(FOLDED_KEY, JSON.stringify([...state.folded]));
  paintFold(project);
}

/* Being taken into a project — a thread opened from a search result, the
   activity page or a URL, or the project's own list — opens it in the tree, so
   the place the reader is in is never hidden inside a fold. The saved fold is
   cleared rather than overridden, so folding it again keeps it shut until the
   next arrival. */
function unfold(key) {
  const project = state.projects.find((one) => one.key === key);
  if (!project || !isFolded(project)) return;
  setFold(project, false);
}

function toggleFold(project) {
  setFold(project, !isFolded(project));
}

function scopeToProject(project) {
  dropSearch();
  state.project = project.key;
  state.scope = "project";
  state.view = "threads";
  unfold(state.project);
  renderTree();
  applyControls();
  runSearch(true);
}

function projectRow(project) {
  const folded = isFolded(project);
  const row = element("div", "proj" + (project.key === state.project ? " current" : "")
    + (folded ? " folded" : ""));
  row.title = project.path +
    (project.unreadable ? `\nunreadable: ${project.unreadable}` : "");
  if (project.unreadable) row.classList.add("bad");
  const fold = element("button", "chev");
  fold.type = "button";
  fold.setAttribute("aria-expanded", folded ? "false" : "true");
  fold.setAttribute("aria-label",
    `${folded ? "Show" : "Hide"} ${project.label}'s threads`);
  fold.append(glyph("chevron"));
  fold.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleFold(project);
  });
  row.append(fold);
  row.append(element("span", "label", project.label));
  row.addEventListener("click", () => scopeToProject(project));
  return row;
}

function threadRow(project, thread, open = false) {
  const row = element("div", "thread" + (open ? " open" : "")
    + (thread.id === state.thread ? " sel" : ""));
  row.title = `${thread.title}\nupdated ${when(thread.updated_at)} · ${thread.messages} message(s)`;
  row.append(element("span", "name", thread.title || "(untitled)"));
  if (thread.running) {
    const spin = element("span", "spin live");
    spin.title = `a turn is running (${thread.turn_state})`;
    spin.append(glyph("spin"));
    row.append(spin);
  }
  row.addEventListener("click", (event) => {
    event.stopPropagation();

    dropSearch();
    state.project = project.key;
    state.thread = thread.id;
    state.scope = "thread";
    runSearch(true);
  });
  return row;
}

function archivedTotal() {
  return state.projects
    .reduce((total, project) => total + (project.archived || []).length, 0);
}

function paintView() {
  const button = $("archived");
  const total = archivedTotal();
  button.classList.toggle("on", state.archived);
  button.setAttribute("aria-pressed", state.archived ? "true" : "false");
  button.title = state.archived
    ? `Archived threads (${total}) — click for the active projects`
    : `Archived threads (${total}) — click to show them`;
  $("arch-count").textContent = total ? String(total) : "";
}

let treeBlocks = new Map();

function projectNodes(project, rows) {
  const row = projectRow(project);
  const block = element("div", "pblock");
  block.append(...rows);
  block.hidden = isFolded(project);
  treeBlocks.set(project.key, {row, block});
  return [row, block];
}

function projectRows(project) {
  if (project.unreadable) return [element("div", "empty", project.unreadable)];
  if (!project.counts.threads) {
    return [element("div", "empty", "No threads yet.")];
  }
  return [...project.open.map((thread) => threadRow(project, thread, true)),
          ...project.closed.map((thread) => threadRow(project, thread))];
}

function renderArchived(box) {
  const projects = state.projects
    .filter((project) => (project.archived || []).length);
  if (!projects.length) {
    box.append(element("div", "empty", "No archived threads."));
    return;
  }
  for (const project of projects) {
    box.append(...projectNodes(project,
      project.archived.map((thread) => threadRow(project, thread))));
  }
}

/* The list is built off the page and swapped in as one mutation, so the
   scroller is never left holding an empty tree, and the offset is put back if
   the engine dropped it anyway. Measured in this build's own engine
   (2026-09-25): a swap of the same rows keeps the offset (183.5 → 183.5), and
   content inserted above the viewport moves it by exactly the inserted height
   (600 → 700 for 100 px) — the engine's own scroll anchoring, which the swap
   leaves alone and which a restore of any sort would fight. */
function renderTree() {
  const tree = $("tree");
  const side = tree.parentElement;
  const parked = side ? side.scrollTop : 0;
  const box = document.createDocumentFragment();
  treeBlocks = new Map();
  paintView();
  if (!state.projects.length) {
    box.append(element("div", "empty", "No Freebuff projects found."));
  } else if (state.archived) {
    renderArchived(box);
  } else {
    for (const project of state.projects.filter((project) =>
        project.unreadable || project.open.length || project.closed.length)) {
      box.append(...projectNodes(project, projectRows(project)));
    }
  }
  tree.replaceChildren(box);
  if (side && parked > 0 && side.scrollTop === 0) side.scrollTop = parked;
}

function when(ms) {
  if (!ms) return "—";
  const date = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
         `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function messageMeta(message) {
  const stamp = when(message.ts);
  return state.debug ? `${stamp} · seq ${message.seq}` : stamp;
}

function activeFilters(answer) {
  const bits = [];

  if (answer.categories
      && answer.categories.join(",") !== DEFAULT_CATEGORIES.join(",")) {
    bits.push(answer.categories
      .map((name) => CATEGORY_LABELS[name] || name).join(" + "));
  }
  if (answer.since || answer.until) {
    bits.push(`dated ${answer.since ? when(answer.since).slice(0, 10) : "…"}` +
      ` – ${answer.until ? when(answer.until).slice(0, 10) : "…"}`);
  }
  if (answer.hide_closed) bits.push("open threads only");
  if (answer.scope === "thread") bits.push("this thread");
  if (answer.scope === "project") bits.push("this project");
  return bits.join(" · ");
}

function renderResults(answer) {
  const pane = $("pane");
  stopClock();
  matches = [];
  $("matchbar").hidden = true;
  pane.replaceChildren();
  const active = activeFilters(answer);
  if (!answer.results.length) {

    const silent = !answer.query;
    $("hits").textContent = silent ? "no query" : "no matches";
    pane.append(element("div", "line dim", silent
      ? "Type something to search. An empty box shows a thread's whole " +
        "conversation once a thread is open and the scope is on it."
      : "Nothing matched. Try Words / Phrase for meaning-shaped queries, or " +
        "Exact text when the string is code." + (active ? ` (${active})` : "")));
    return;
  }
  const threads = new Set(answer.results.map((row) => row.thread_id));
  const projects = new Set(answer.results.map((row) => row.project));
  const hits = element("span", null,
    `${answer.results.length} of ${answer.total.toLocaleString()} part(s) · ` +
    `${threads.size} thread(s) · ${projects.size} project(s) · ` +
    `${answer.elapsed_ms.toFixed(1)} ms · ` +
    `${answer.mode === "exact" ? "exact text" : "words / phrase"} · live`);
  $("hits").replaceChildren(hits);
  if (active) $("hits").append(element("span", "active", ` · ${active}`));

  let seen = null;
  for (const row of answer.results) {
    if (row.thread_id !== seen) {
      seen = row.thread_id;
      const head = element("div", "res-head");
      head.textContent = `${row.project} · ${row.thread_title}`;
      pane.append(head);
    }
    const turn = element("div", "turn");
    const message = element("div",
      "msg" + (row.role === "user" ? " mine" : " theirs"));
    message.append(element("div", "meta", messageMeta(row)));

    message.append(row.kind === "text" ? resultBubble(row) : resultEntry(row));
    turn.append(message);
    turn.addEventListener("click", () => openResult(row));
    pane.append(turn);

    if (!answer.browsing) {
      matches.push({node: message, seq: row.seq, part: row.part_index});
    }
  }

  if (matches.length) showMatch(0, false);
}

function snippetNode(row) {
  const line = element("div", "line");
  line.innerHTML = row.snippet;
  return line;
}

function resultBubble(row) {
  const bubble = element("div", "bubble");
  bubble.append(snippetNode(row));
  return bubble;
}

function resultLabel(row) {
  if (row.kind === "reasoning") return "Thinking";
  return row.label || toolLabelFallback(row.name);
}

function resultDetail(row) {

  if (row.kind === "reasoning") return row.label || "";
  return row.detail || "";
}

function resultMeta(row) {
  if (row.kind !== "tool") return "";
  const bits = [row.name];
  if (row.half === "result") bits.push("the result");
  return bits.join(" · ");
}

function resultEntry(row) {
  const details = collapsible({
    icon: row.icon, label: resultLabel(row), detail: resultDetail(row),
    body: snippetNode(row), meta: resultMeta(row), boxClass: "body",
  });
  details.open = true;
  return details;
}

function openResult(row) {
  pendingJump = {seq: row.seq, part: row.part_index};
  matchIndex = 0;
  state.project = row.project_key;
  state.thread = row.thread_id;
  state.scope = "thread";
  state.view = null;
  commitQuery(false);
  openThread("match");
}

const SVG_NS = "http://www.w3.org/2000/svg";
const GLYPHS = {
  chevron: {width: 1.8, shapes: [
    ["path", {d: "M4.4 6.7 L8 10.3 L11.6 6.7"}],
  ]},
  think: {filled: true, shapes: [
    ["path", {d: "M8 1.6 L9.1 5.6 L13.1 6.7 L9.1 7.8 L8 11.8 L6.9 7.8 L2.9 6.7 L6.9 5.6 Z"}],
    ["path", {d: "M12.4 10.4 L13 12.2 L14.8 12.8 L13 13.4 L12.4 15.2 L11.8 13.4 L10 12.8 L11.8 12.2 Z"}],
  ]},
  todos: {width: 1.4, shapes: [
    ["rect", {x: 3, y: 1.8, width: 10, height: 12.4, rx: 2.4}],
    ["path", {d: "M5.4 5.2 L6.2 6 L7.6 4.6"}],
    ["path", {d: "M8.6 5.2 H10.6"}],
    ["path", {d: "M5.4 8.4 L6.2 9.2 L7.6 7.8"}],
    ["path", {d: "M8.6 8.4 H10.6"}],
    ["path", {d: "M5.4 11.6 H10.6"}],
  ]},
  run: {width: 1.5, shapes: [
    ["rect", {x: 1.8, y: 2.8, width: 12.4, height: 10.4, rx: 2.6}],
    ["path", {d: "M4.6 6.2 L6.8 8.2 L4.6 10.2"}],
    ["path", {d: "M8.4 10.2 H11.2"}],
  ]},
  globe: {width: 1.35, shapes: [
    ["circle", {cx: 8, cy: 8, r: 6.2}],
    ["path", {d: "M1.8 8 H14.2"}],
    ["path", {d: "M8 1.8 C10.6 4.4 10.6 11.6 8 14.2"}],
    ["path", {d: "M8 1.8 C5.4 4.4 5.4 11.6 8 14.2"}],
  ]},
  read: {width: 1.5, shapes: [
    ["path", {d: "M3.6 1.8 H9.2 L12.6 5.2 V14.2 H3.6 Z"}],
    ["path", {d: "M9.2 1.8 V5.2 H12.6"}],
    ["path", {d: "M5.8 8.4 H10.4"}],
    ["path", {d: "M5.8 11 H9"}],
  ]},
  edit: {width: 1.35, shapes: [
    ["path", {d: "M3.6 1.8 H8.6 L12 5.2 V14.2 H3.6 Z"}],
    ["path", {d: "M8.6 1.8 V5.2 H12"}],
    ["path", {d: "M8.6 10.6 L13.4 5.8 L14.6 7 L9.8 11.8 L8.2 12.2 Z"}],
  ]},
  search: {width: 1.5, shapes: [
    ["circle", {cx: 7, cy: 7, r: 4.4}],
    ["path", {d: "M10.3 10.3 L14.2 14.2"}],
  ]},
  list: {width: 1.4, shapes: [
    ["path", {d: "M2.2 4.4 H4.6"}], ["path", {d: "M6.6 4.4 H13.6"}],
    ["path", {d: "M2.2 8 H4.6"}], ["path", {d: "M6.6 8 H13.6"}],
    ["path", {d: "M2.2 11.6 H4.6"}], ["path", {d: "M6.6 11.6 H13.6"}],
  ]},
  done: {width: 1.5, shapes: [
    ["circle", {cx: 8, cy: 8, r: 6.2}],
    ["path", {d: "M5.1 8.3 L7.2 10.4 L11.2 5.9"}],
  ]},
  spin: {width: 1.8, shapes: [
    ["circle", {cx: 8, cy: 8, r: 6.2, "stroke-dasharray": "29.2 9.74"}],
  ]},
  bolt: {filled: true, shapes: [
    ["path", {d: "M9.6 1.2 L3.4 9 H7.2 L6.4 14.8 L12.6 7 H8.8 Z"}],
  ]},
  tool: {width: 1.4, shapes: [
    ["rect", {x: 1.8, y: 1.8, width: 12.4, height: 12.4, rx: 3}],
  ]},
};

function glyph(icon) {
  const shape = GLYPHS[icon] || GLYPHS.tool;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  if (shape.filled) {
    svg.setAttribute("fill", "currentColor");
  } else {
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", String(shape.width || 1.4));
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
  }
  for (const [tag, attrs] of shape.shapes) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attrs)) {
      node.setAttribute(name, String(value));
    }
    svg.append(node);
  }
  return svg;
}

function collapsible(parts) {
  const details = element("details", "block");
  const head = element("summary");
  const mark = element("span", "mark");
  mark.append(glyph(parts.icon));
  head.append(mark);
  head.append(element("span", "lab", parts.label || "…"));
  head.append(element("span", "sum", parts.detail || ""));
  head.title = parts.meta || "";
  details.append(head);

  const body = parts.body;
  const box = element("div",
    parts.boxClass || (body instanceof Node ? "body prose" : "body"));
  if (body instanceof Node) box.append(body);
  else box.textContent = body === undefined || body === null ? "" : String(body);
  details.append(box);
  if (parts.meta) details.append(element("div", "meta", parts.meta));
  return details;
}

function markdownNode(part) {
  const node = element("div", "md");
  if (part.html) node.innerHTML = part.html;
  else node.textContent = part.text || "";
  return node;
}

function patchNode(patch) {
  const body = element("div", "patch");
  const text = (patch && patch.text) || "";
  for (const line of String(text).split("\n")) {
    let cls = "";
    if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("Index:") || line.startsWith("===") ||
             line.startsWith("---") || line.startsWith("+++")) cls = "file-head";
    else if (line.startsWith("+")) cls = "plus";
    else if (line.startsWith("-")) cls = "minus";
    body.append(element("span", cls, line));
  }
  return body;
}

function diffRow(change) {
  const row = element("details", "diffile");
  const head = element("summary");
  const mark = element("span", "mark");
  mark.append(glyph("edit"));
  head.append(mark);
  head.append(element("span", "path", change.path || "?"));
  if (change.status && change.status !== "modified") {
    head.append(element("span", "kind", change.status));
  }
  const counts = element("span", "counts");
  if (change.adds) counts.append(element("span", "add", `+${change.adds}`));
  if (change.dels) counts.append(element("span", "del", `−${change.dels}`));
  head.append(counts);
  row.append(head);
  row.append(patchNode(change.patch));
  return row;
}

function diffCard(part) {
  const files = part.files || [];
  if (!files.length) return null;

  const card = element("details", "diffset");
  const head = element("summary");
  head.append(element("span", "card-title",
    `Agent changed ${files.length} file${files.length === 1 ? "" : "s"}`));
  const totals = element("span", "card-totals");
  totals.append(element("span", "add", `+${part.adds || 0}`));
  totals.append(element("span", "del", `−${part.dels || 0}`));
  head.append(totals);
  card.append(head);

  const list = element("ul", "files");
  for (const change of files) list.append(diffRow(change));
  card.append(list);

  if (part.part !== undefined && part.part !== null) card.dataset.part = part.part;
  return card;
}

function toolLabelFallback(name) {
  const words = String(name || "tool").split("_").filter(Boolean);
  if (!words.length) return "tool";
  const head = words[0].charAt(0).toUpperCase() + words[0].slice(1).toLowerCase();
  return [head, ...words.slice(1).map((word) => word.toLowerCase())].join(" ");
}

function partLabel(part) {
  if (part.kind === "reasoning") return "Thinking";
  return part.label || toolLabelFallback(part.name);
}

function partDetail(part) {
  if (part.kind === "reasoning") return part.label || "";
  return part.detail || "";
}

function entryMeta(part) {
  if (part.kind !== "tool") return "";
  const bits = [part.name];
  if (part.status) bits.push(part.status);
  if (part.exit_code !== null && part.exit_code !== undefined) {
    bits.push(`exit ${part.exit_code}`);
  }
  return bits.join(" · ");
}

function blockText(part) {
  if (part.kind === "tool") {
    return `${part.input.text}\n\n— output —\n${part.output.text}`;
  }
  return markdownNode(part);
}

let lastThread = null;

function partEntry(part) {
  const meta = entryMeta(part);
  const node = collapsible({
    icon: part.icon, label: partLabel(part), detail: partDetail(part),
    body: blockText(part), meta: meta,
  });

  if (part.part !== undefined && part.part !== null) node.dataset.part = part.part;
  return node;
}

function messageBlock(message) {
  const messageRow = element("div",
    "msg" + (message.role === "user" ? " mine" : " theirs"));

  messageRow.dataset.seq = message.seq;
  messageRow.append(element("div", "meta", messageMeta(message)));

  let open = null;
  function prose() {
    if (!open) {
      open = element("div", "bubble");
      messageRow.append(open);
    }
    return open;
  }

  for (const part of message.blocks || []) {
    if (part.kind === "reasoning" || part.kind === "tool") {
      open = null;
      messageRow.append(partEntry(part));
      continue;
    }
    if (part.kind === "text") {
      const bubble = prose();
      const line = markdownNode(part);
      line.classList.add("line");
      if (part.part !== undefined && part.part !== null) {
        line.dataset.part = part.part;
      }
      bubble.append(line);
      if (part.truncated) bubble.append(element("div", "note", "(truncated)"));
      continue;
    }
    open = null;
    if (part.kind === "changes") {
      const card = diffCard(part);
      if (card) messageRow.append(card);
    } else if (part.kind === "notice") {
      const node = noticeBlock(part.text);
      if (part.part !== undefined && part.part !== null) {
        node.dataset.part = part.part;
      }
      messageRow.append(node);
    }
  }

  for (const attachment of message.attachments || []) {
    const chip = element("span", "chip", `${attachment.name} (${attachment.kind})`);
    chip.title = attachment.path || attachment.name;
    prose().append(chip);
  }

  if (message.role === "assistant") {
    const foot = element("div", "foot-note", replyFooter(message));
    foot.title = message.footer_detail || "";
    messageRow.append(foot);
  }
  return messageRow;
}

function replyFooter(message) {
  const bits = [];
  if (message.elapsed_text) {
    bits.push(message.elapsed_upper ? `\u2264 ${message.elapsed_text}` : message.elapsed_text);
  }
  bits.push(message.usage_text || "usage not recorded");
  return bits.join(" \u00b7 ");
}

let clock = null;

function stopClock() {
  if (clock !== null) {
    clearInterval(clock);
    clock = null;
  }
}

function elapsedText(ms) {
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  const total = Math.round(seconds);
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function runningClock(thread) {
  const line = element("div", "foot-note live");
  const started = thread.started_at || Date.now();
  const tick = () => {
    line.textContent = `\u25cc running \u00b7 ${elapsedText(Date.now() - started)} in`;
  };
  tick();
  line.title = `a turn is running in this thread (${thread.turn_state}); the reply has no ` +
    `row yet, so this counts from the prompt at ${when(started)}`;
  stopClock();
  clock = setInterval(tick, 1000);
  return line;
}

function noticeBlock(notice) {
  return element("div", "notice", notice);
}

function renderThread(thread, opts) {
  const options = opts || {};
  const pane = $("pane");
  pane.replaceChildren();
  $("hits").textContent =
    `${thread.message_count} message(s) · ${thread.turns.length} turn(s) shown` +
    (thread.in_flight ? " · a turn is running" : "");

  const head = element("div", "thread-head");
  head.append(element("h1", null, thread.title || "(untitled)"));
  const meta = element("div", "role");
  meta.textContent = `${thread.project} · ${thread.status} · created ${when(thread.created_at)}` +
    ` · updated ${when(thread.updated_at)} · ${thread.message_count} message(s)`;
  head.append(meta);

  if (state.debug) {
    const id = element("div", "meta",
      `${thread.project_path} · thread ${thread.thread_id}`);
    id.style.color = "var(--quiet)";
    id.style.fontSize = "11.5px";
    id.style.fontFamily = "var(--mono)";
    head.append(id);
  }
  pane.append(head);

  let lastRow = null;
  for (const turn of thread.turns) {
    const wrap = element("div", "turn");
    for (const message of turn.messages) {
      lastRow = messageBlock(message);
      wrap.append(lastRow);
    }
    pane.append(wrap);
  }

  if (thread.in_flight && lastRow) lastRow.append(runningClock(thread));
  else stopClock();
  applyMatches(thread, thread.found ? thread.found.stops : [], options);

  if (options.atEnd) scrollToEnd();
  else if (options.restore !== undefined && !matches.length) {
    const scroller = $("pane").closest(".main");
    if (scroller) scroller.scrollTop = options.restore;
  }
}

function scrollToEnd() {
  const scroller = $("pane").closest(".main");
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
}

const reading = new Map();

function rememberReading() {
  const scroller = $("pane").closest(".main");
  if (!scroller) return;
  if (!state.thread || !lastThread) return;
  if (lastThread.thread_id !== state.thread) return;
  reading.set(state.thread, scroller.scrollTop);
}

let matches = [];
let matchIndex = 0;
let pendingJump = null;

function markNeedles(root, needles) {
  if (!root) return;
  const wanted = (needles || [])
    .map((one) => String(one).toLowerCase()).filter(Boolean);
  if (!wanted.length) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts = [];
  let node = walker.nextNode();
  while (node) {
    if (!(node.parentElement && node.parentElement.closest("mark"))) {
      texts.push(node);
    }
    node = walker.nextNode();
  }
  for (const text of texts) {
    const body = text.nodeValue;
    const lowered = body.toLowerCase();
    const spans = [];
    for (const needle of wanted) {
      let at = lowered.indexOf(needle);
      while (at >= 0) {
        spans.push([at, at + needle.length]);
        at = lowered.indexOf(needle, at + needle.length);
      }
    }
    if (!spans.length) continue;
    spans.sort((one, two) => one[0] - two[0] || one[1] - two[1]);
    const merged = [];
    for (const span of spans) {
      const last = merged[merged.length - 1];
      if (last && span[0] <= last[1]) last[1] = Math.max(last[1], span[1]);
      else merged.push([span[0], span[1]]);
    }
    const pieces = [];
    let cut = 0;
    for (const [start, end] of merged) {
      if (start > cut) pieces.push(document.createTextNode(body.slice(cut, start)));
      pieces.push(element("mark", "hit", body.slice(start, end)));
      cut = end;
    }
    if (cut < body.length) pieces.push(document.createTextNode(body.slice(cut)));
    const parent = text.parentNode;
    for (const piece of pieces) parent.insertBefore(piece, text);
    parent.removeChild(text);
  }
}

function stopNode(scope, stop) {
  const message = scope.querySelector(`.msg[data-seq="${stop.seq}"]`);
  if (!message) return null;
  const node = message.querySelector(`[data-part="${stop.part}"]`);
  return node || null;
}

function applyMatches(thread, stops, opts) {
  const options = opts || {};
  const needles = (thread.found && thread.found.needles) || [];
  const asked = pendingJump;
  pendingJump = null;
  matches = [];
  for (const stop of stops || []) {
    const node = stopNode($("pane"), stop);
    if (!node) continue;
    const message = node.closest(".msg");
    matches.push({seq: stop.seq, part: stop.part});
    message.classList.add("hit-msg");

    if (node.tagName === "DETAILS") node.open = true;
    markNeedles(node.matches("details.block") ? node.querySelector(".body") : node,
                needles);
  }
  if (!matches.length) {
    paintMatchBar();
    return;
  }
  const at = asked
    ? matches.findIndex((one) => one.seq === asked.seq && one.part === asked.part)
    : -1;
  matchIndex = Math.min(matchIndex, matches.length - 1);

  showMatch(at >= 0 ? at : matchIndex, at >= 0 || options.reveal === true);
}

function matchMessage(entry) {
  if (!entry) return null;
  if (entry.node) return entry.node;
  const node = stopNode($("pane"), entry);
  return (node && node.closest(".msg")) || null;
}

function showMatch(index, scroll) {
  if (!matches.length) return;
  matchIndex = (index + matches.length) % matches.length;

  matches.forEach((one) => {
    const message = matchMessage(one);
    if (message) message.classList.remove("hit-now");
  });
  const currentMessage = matchMessage(matches[matchIndex]);
  if (currentMessage) currentMessage.classList.add("hit-now");

  paintMatchBar();
  if (scroll) revealMatch();
}

function revealMatch() {
  const entry = matches[matchIndex] || {};
  const current = entry.node || stopNode($("pane"), entry);
  const pane = $("pane");
  const scroller = pane.closest(".main");
  if (!current || !scroller) return;
  const band = $("bar");
  const pill = $("matchbar");
  const floating = band.offsetHeight + (pill.hidden ? 0 : pill.offsetHeight + 12);
  const top = current.getBoundingClientRect().top
    - scroller.getBoundingClientRect().top + scroller.scrollTop - floating - 12;
  scroller.scrollTop = Math.max(0, top);
}

function paintMatchBar() {
  const bar = $("matchbar");
  if (!matches.length) {
    bar.hidden = true;
    return;
  }
  const count = matches.length;
  bar.hidden = false;
  $("matchcount").textContent = `${count} match${count === 1 ? "" : "es"} found`;
  $("matchpos").textContent = `${matchIndex + 1} / ${count}`;
  $("match-prev").disabled = count < 2;
  $("match-next").disabled = count < 2;
}

function dropSearch() {
  state.query = "";
  state.mode = "words";
  state.categories = [...DEFAULT_CATEGORIES];
  state.since = "";
  state.until = "";
  state.hideClosed = false;
  state.scope = "all";

  state.thread = null;
  state.view = null;
  pendingJump = null;
  matchIndex = 0;
  $("q").value = "";
}

function agoText(ms) {
  if (!ms) return "—";
  const seconds = Math.max(0, Date.now() - ms) / 1000;
  if (seconds < 60) return "now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d`;
  const months = Math.floor(days / 30);
  return months < 12 ? `${months}mo` : `${Math.floor(months / 12)}y`;
}

function threadCard(project, thread, open) {
  const card = element("div", "tcard" + (open ? "" : " shut"));
  card.title = `${thread.title || "(untitled)"}\n${open ? "open" : "closed"}` +
    ` · ${thread.messages} message(s) · updated ${when(thread.updated_at)}` +
    (thread.running ? `\na turn is running (${thread.turn_state})` : "");
  const text = element("div", "ttext");
  text.append(element("div", "ttitle", thread.title || "(untitled)"));
  text.append(element("div", "tdetail",
    `${project.label} · ${thread.messages} message(s)`));
  card.append(text);
  if (thread.running) {
    const spin = element("span", "spin live");
    spin.title = `a turn is running (${thread.turn_state})`;
    spin.append(glyph("spin"));
    card.append(spin);
  }
  card.append(element("span", "tage", agoText(thread.updated_at)));
  card.addEventListener("click", () => {
    dropSearch();
    state.project = project.key;
    state.thread = thread.id;
    state.scope = "thread";
    runSearch(true);
  });
  return card;
}

function renderProjectThreads(project) {
  const pane = $("pane");
  stopClock();
  matches = [];
  $("matchbar").hidden = true;
  pane.replaceChildren();
  const groups = state.archived
    ? [["Archived", project.archived, false]]
    : [["Recent", project.open, true], ["Closed", project.closed, false]];
  const total = groups.reduce((sum, group) => sum + group[1].length, 0);
  const head = element("div", "threads-head");
  head.append(element("h1", null, project.label));
  head.append(element("div", "role",
    `${total} thread(s) · ${project.counts.messages} message(s)` +
    (state.archived ? " · archived" :
      ` · ${project.open.length} open · ${project.closed.length} closed`)));
  pane.append(head);
  for (const [label, rows, open] of groups) {
    if (!rows.length) continue;
    pane.append(element("div", "res-head", label));
    const list = element("div", "tlist");
    for (const thread of [...rows].sort((one, two) => two.updated_at - one.updated_at)) {
      list.append(threadCard(project, thread, open));
    }
    pane.append(list);
  }
  if (!total) pane.append(element("div", "line dim", "No threads yet."));
  $("hits").textContent =
    `${total} thread(s) · ${project.counts.messages} message(s)`;
  const scroller = pane.closest(".main");
  if (scroller) scroller.scrollTop = 0;
}

function newSearch() {
  stopClock();
  state.query = "";
  state.scope = "all";
  state.project = null;
  state.thread = null;
  pendingJump = null;
  matchIndex = 0;
  writeRoute(true);
  applyControls();
  renderTree();
  $("pane").replaceChildren();
  $("q").focus();
  runSearch();
}

let threadRequest = 0;

/* Four arrivals, and each one is a different place to land: a visit lands at
   the end of the thread, or at the match when a search is what brought the
   reader here; Back and Forward land where that thread was left; and a redraw
   of the thread already in the pane does not move the reader at all. */
function landing(asked, thread, scroller, shown) {
  if (asked === "end") return {atEnd: true};
  if (asked === "match") return {atEnd: !liveQuery()};
  if (asked === "place") {
    const place = reading.get(thread.thread_id);
    return place === undefined ? {atEnd: true} : {restore: place};
  }
  if (!shown || !scroller) return {atEnd: true};
  const bottom = scroller.scrollHeight - scroller.clientHeight;
  if (scroller.scrollTop >= bottom - 8) return {atEnd: true};
  return {restore: scroller.scrollTop};
}

async function openThread(arrival) {
  if (!state.thread) return;
  stopClock();
  const token = ++threadRequest;
  const asked = arrival || "hold";
  if (asked !== "hold") unfold(state.project);
  const scroller = $("pane").closest(".main");
  const shown = lastThread !== null && lastThread.thread_id === state.thread;
  const leaving = lastThread !== null && scroller !== null
    ? {id: lastThread.thread_id, at: scroller.scrollTop} : null;
  matchIndex = 0;
  writeRoute(asked === "end" || asked === "match");
  applyControls(false);
  renderTree();
  $("hits").textContent = "loading thread…";
  try {

    const thread = await api("/api/thread", {
      project: state.project, thread: state.thread,
      q: liveQuery(), mode: state.mode,
      category: state.categories.join(","),
      since: state.since, until: state.until,
      hide_closed: state.hideClosed ? "1" : "",
    });
    if (token !== threadRequest) return;
    lastThread = thread;

    if (leaving && leaving.id !== thread.thread_id) reading.set(leaving.id, leaving.at);
    renderThread(thread, {...landing(asked, thread, scroller, shown), reveal: true});
  } catch (error) {
    if (token !== threadRequest) return;
    $("pane").replaceChildren(element("div", "line dim", String(error.message)));
    $("hits").textContent = "";
  }
}

async function runSearch(push, arrival) {
  if (state.scope === "thread" && state.thread) {
    await openThread(arrival || (push === true ? "end" : "hold"));
    return;
  }
  if (state.view === "threads" && state.project && !liveQuery()) {
    const project = state.projects.find((one) => one.key === state.project);
    if (project) {
      writeRoute(push === true);
      renderProjectThreads(project);
      return;
    }
  }
  writeRoute(push === true);
  $("hits").textContent = "searching…";
  try {
    const answer = await api("/api/search", {
      q: liveQuery(), mode: state.mode, scope: state.scope,
      category: state.categories.join(","),
      project: state.project, thread: state.thread,
      since: state.since, until: state.until,
      hide_closed: state.hideClosed ? "1" : "",
    });
    setSchemaLimited(answer.schema_limited);
    renderResults(answer);
  } catch (error) {
    $("pane").replaceChildren(element("div", "line dim", String(error.message)));
    $("hits").textContent = "that search could not run";
  }
}

let live = null;

function searchSoon() {
  if (live !== null) clearTimeout(live);
  live = setTimeout(() => {
    live = null;
    runSearch();
  }, LIVE_SEARCH_MS);
}

let treeSignature = "";

let treeRequest = 0;

async function loadProjects(force) {
  const token = ++treeRequest;
  let projects = [];
  try {
    projects = (await api("/api/projects")).projects;
  } catch (error) {
    if (token !== treeRequest) return;

    if (state.projects.length) return;
    $("pane").replaceChildren(element("div", "line dim", String(error.message)));
    renderTree();
    return;
  }

  if (token !== treeRequest) return;

  const signature = JSON.stringify(projects);
  if (!force && signature === treeSignature) return;
  treeSignature = signature;
  state.projects = projects;
  renderTree();
}

function setSchemaLimited(limited, details) {
  state.schemaLimited = !!limited;
  if (details && details.length) state.schemaDetails = details;
  paintFooter();
}

function paintFooter() {
  const status = state.status;
  const caps = (status && status.sqlite) || {};
  const nodes = [];
  if (status) {
    nodes.push(element("span", null,
      `build ${status.build} · sqlite ${caps.sqlite_version}` +
      ` · json ${caps.json ? "yes" : "no"} · fts5 ${caps.fts5 ? "yes" : "no"}`));
  }
  if (state.schemaLimited) {
    const warn = element("span", "warn",
      "Freebuff database schema changed, results may be limited.");
    warn.title = state.schemaDetails.join(" · ");
    nodes.push(warn);
  }
  $("foot").replaceChildren(...nodes);
}

async function loadStatus() {
  try {
    state.status = await api("/api/status");
  } catch (error) {
    return;
  }
  const schema = state.status.schema || {};
  setSchemaLimited(schema.limited, schema.details);
}

let stream = null;

async function onServerChange(message) {
  let payload = {};
  try {
    payload = JSON.parse(message.data);
  } catch (error) {
    payload = {};
  }
  const changes = payload.changes || [];
  await loadProjects();
  if (state.thread && lastThread && lastThread.thread_id === state.thread
      && changes.some((line) => line.includes(state.thread))) {
    openThread();
  }
}

function watchStream() {
  const watch = (state.status && state.status.watch) || {};
  if (!watch.stream || !window.EventSource || stream) return;
  stream = new EventSource("/api/events");
  stream.onmessage = (message) => {
    onServerChange(message);
  };

  stream.onopen = () => {
    loadProjects();
  };
}

function wire() {
  $("archived").addEventListener("click", () => {
    state.archived = !state.archived;
    renderTree();
    if (state.view === "threads") runSearch();
  });
  $("search-open").addEventListener("click", newSearch);
  $("bar").addEventListener("submit", (event) => {
    event.preventDefault();
    commitSearch();
  });

  $("q").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    commitSearch();
  });
  $("q").addEventListener("input", () => {
    searchSoon();
  });
  $("q").addEventListener("blur", () => {
    commitQuery(true);
  });
  $("mode").addEventListener("change", () => {
    state.mode = $("mode").value;
    refineSearch();
  });
  $("scope").addEventListener("change", () => {
    state.scope = $("scope").value;
    state.view = null;
    refineSearch();
  });
  for (const name of CATEGORIES) {
    $(`cat-${name}`).addEventListener("change", () => {
      state.categories = CATEGORIES.filter((one) => $(`cat-${one}`).checked);
      paintOptions();
      refineSearch();
    });
  }
  for (const field of ["since", "until"]) {
    $(field).addEventListener("change", () => {
      state[field] = $(field).value;
      refineSearch();
    });
  }
  $("hide-closed").addEventListener("change", () => {
    state.hideClosed = $("hide-closed").checked;
    paintOptions();
    refineSearch();
  });
  $("clear-all").addEventListener("click", () => {
    state.categories = [...DEFAULT_CATEGORIES];
    state.since = "";
    state.until = "";
    state.hideClosed = false;
    state.mode = "words";
    applyControls();
    refineSearch();
  });

  const scroller = $("pane").closest(".main");
  if (scroller) scroller.addEventListener("scroll", rememberReading);

  $("gear").addEventListener("click", (event) => {
    event.stopPropagation();
    toggleMenu($("menu").hidden);
  });
  for (const choice of ["light", "dark", "system"]) {
    $(`theme-${choice}`).addEventListener("click", () => {
      state.theme = choice;
      savePreference(THEME_KEY, choice);
      applyTheme();
      applyControls();
    });
  }
  $("debug").addEventListener("change", () => {
    state.debug = $("debug").checked;
    savePreference(DEBUG_KEY, state.debug ? "1" : "0");

    applyControls();
    if (state.thread) renderThreadNow();
    else runSearch();
  });
  $("menu").addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", () => toggleMenu(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") toggleMenu(false);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadProjects();
  });

  $("match-prev").addEventListener("click", () => showMatch(matchIndex - 1, true));
  $("match-next").addEventListener("click", () => showMatch(matchIndex + 1, true));

  window.addEventListener("hashchange", () => {
    readRoute();
    applyControls();
    renderTree();
    if (state.thread) openThread("place");
    else runSearch();
  });
}

function toggleMenu(open) {
  $("menu").hidden = !open;
  $("gear").setAttribute("aria-expanded", open ? "true" : "false");
}

function renderThreadNow() {

  if (lastThread) renderThread(lastThread, {atEnd: false, reveal: false});
}

function measureBand() {
  const band = $("bar");
  if (band) {
    document.documentElement.style.setProperty("--bar-h", `${band.offsetHeight}px`);
  }
}

async function main() {
  loadPreferences();
  wire();
  readRoute();
  applyControls();
  measureBand();

  if (window.ResizeObserver) new ResizeObserver(measureBand).observe($("bar"));
  await loadProjects(true);
  await loadStatus();
  watchStream();
  if (state.thread) await openThread("end");
  else await runSearch();
}

main();
