// Nodes dashboard. Live from SSE; the graph view is FalkorDB's own canvas (vendored @falkordb/canvas 0.3.3).
import "/static/vendor/falkordb-canvas/falkordb-canvas.js";
import {Live} from "/static/live.js";
import {animalSVG, colorFor} from "/static/animals.js";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const q = s => encodeURIComponent(s);
const colors = {};
const color = a => colors[a] ??= colorFor(a);  // one cheerful colour (and animal) per agent, in order of appearance
const nameOf = a => String(a || "").replace(/-[0-9a-f]{6}(-[0-9a-f]{4})?$/, m => m.length > 7 ? " (subagent)" : "");
const short = u => { if (!u) return ""; const m = String(u).match(/#(.+)$/), f = String(u).split(":").pop().split("#")[0].split("/").pop();
  if (!m) return f; const s = m[1]; return s === "imports" ? `imports of ${f}` : s.startsWith("module@") ? `${f} block ${s.slice(7)}`
  : s.endsWith(".__body__") ? `${s.slice(0, -9)} fields` : s; };
const ago = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${s | 0}s ago` : s < 3600 ? `${s / 60 | 0}m ago` : `${s / 3600 | 0}h ago`; };
const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"});
const UNIT_RE = /(fn|region|file|class):[^ ,]+/g;
// theme: light by default, dark on request; the canvas reads its colours from the same CSS variables
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
let T = {};
function readTheme() {
  T = {ink: css("--ink"), ink2: css("--ink2"), calls: css("--edge-calls"), contains: css("--edge-contains"), raw: css("--edge-raw"),
       spark: css("--spark"), sparkOff: css("--spark-off"),
       stroke: document.documentElement.dataset.theme === "dark" ? "rgba(0,0,0,.35)" : "rgba(0,0,0,.18)"};
}
readTheme();

let ws = new URLSearchParams(location.search).get("ws") || "", events = [], live = true, cursor = 0, es = null,
    state = {holds: {}, last: {}, done: {}, feed: []}, playing = null, showAll = false, laneTask = {}, facts = [],
    calls = [], callFilter = "all", selCall = null, graph = {nodes: [], edges: []}, layout = "force", lit = new Set(), litTimer = null;

// ---------------------------------------------------------------- the story: one event -> plain words
function story(e, prev) {
  const p = (e.path || "").split(" "), A = nameOf(e.agent);
  if (e.kind === "decision" && e.advisory) {  // multi-hop overlap: a warning to look, never a block
    const theirs = p[p.length - 3], owner = p[p.length - 1];
    return e.verdict === "acknowledged"
      ? {cls: "go", text: `${A} confirmed its change can't affect ${short(theirs)} (${nameOf(owner)}'s): "${e.reason || ""}".`}
      : {cls: "wait", text: `Possible overlap (advisory): ${A}'s change to ${short(p[0])} reaches ${short(theirs)}, which ${nameOf(owner)} ${p[p.length - 2] === "-HELD_BY->" ? "holds" : "plans to change"}. ${A} must confirm or coordinate.`};
  }
  if (e.kind === "decision") {
    if (e.verdict === "proceed") { const units = ((e.path || "").split("-HOLDS-> ")[1] || "its change").split(", ").map(short).join(", ");
      return {cls: "go", text: prev[e.agent] === "defer" ? `${A} moved on to ${units}.` : `${A} went ahead with ${units}.`}; }
    if (e.verdict === "defer") return {cls: "wait", text: `${A} tried to edit ${short(p[2])}. ${nameOf(p[4])} is working on it, so Nodes asked ${A} to wait.`};
    if (e.verdict === "handoff") return {cls: "hand", text: `${nameOf(p[0])} finished. Nodes handed ${short(p[4])} to ${nameOf(p[6])}, who merges that work first.`};
    if (e.verdict === "escalate") return (e.path || "").includes("RELIES_ON")
      ? {cls: "block", text: `${A} relied on a claim the code doesn't back up. Nodes stopped and asked a human.`}
      : {cls: "block", text: `${nameOf(p[0])} and ${nameOf(p[2])} were waiting on each other. Nodes asked a human to decide.`};
  }
  if (e.kind === "fact" && e.status === "observed") return {cls: /fail|error/i.test(e.text || "") ? "block" : "go",
    text: `${A} ran checks. ${e.text || ""}`};
  if (e.kind === "fact") return e.status === "source-checked" ? {cls: "go", text: `${A} shared a fact; Nodes checked it against the code.`}
    : e.status === "extracted" ? null : {cls: "block", text: `${A} shared a claim Nodes could not verify. It is marked unverified.`};
  if (e.kind === "done") return {cls: "hand", text: `${A} finished${e.commit ? ` (commit ${String(e.commit).slice(0, 7)})` : ""}. Its code can be handed on.`};
  if (e.kind === "release") return {cls: "hand", text: `${A} released ${(e.units || []).length || "its"} unit(s)${(e.notify || []).length ? `; ${e.notify.map(nameOf).join(", ")} told at once` : ""}.`};
  if (e.kind === "intent") return {cls: "", text: `${A} said what it plans to change.`};
  if (e.kind === "query") return {cls: "", text: `${A} asked the graph: ${e.summary || e.tool}.`};
  if (e.kind === "join") return {cls: "", text: `${A} joined.`};
  if (e.kind === "subagent-start") return {cls: "", text: `${A} started${e.task ? `: ${e.task}` : ""}.`};
  if (e.kind === "merge-snapshot") return {cls: "hand", text: `${A} is merging; Nodes saved a snapshot of the graph first.`};
  if (e.kind === "stale-write") return {cls: "block", text: `${A} wrote code it no longer held. Nodes flagged it as stale.`};
  return null;
}

function rebuild(n) {
  const holds = {}, last = {}, prev = {}, done = {}, feed = [], touched = {}; let conf = 0, hand = 0;
  for (const e of events.slice(0, n)) {
    const s = story(e, prev); if (s) feed.push({...s, e});
    if (e.agent && !(e.agent in last) && e.agent !== "importer") last[e.agent] = "";
    if (e.kind === "decision") {
      prev[e.agent] = e.verdict; if (s) last[e.agent] = s.text;
      if (e.verdict === "defer" || e.verdict === "escalate") conf++;
      if (e.verdict === "handoff") hand++;
      const units = e.verdict === "proceed" ? ((e.path || "").split("-HOLDS-> ")[1] || "").split(", ")
        : e.verdict === "handoff" ? [(e.path || "").split(" ")[4]] : [];
      for (const u of units.filter(Boolean)) { for (const a in holds) holds[a].delete(u); (holds[e.agent] ??= new Set()).add(u); touched[u] = e.agent; }
    }
    if (e.kind === "done") done[e.agent] = e.commit || true;
    if (e.kind === "release" || e.kind === "session-end") holds[e.agent]?.clear();
  }
  return {holds, last, done, feed, conf, hand, touched};
}
const ownerOf = u => { for (const a in state.holds) if (state.holds[a].has(u)) return a; return null; };

// ---------------------------------------------------------------- overview
function renderOverview(fresh) {
  const agents = Object.keys(state.last);
  $("k-agents").textContent = agents.length; $("k-hand").textContent = state.hand; $("k-conf").textContent = state.conf;
  $("intro").innerHTML = agents.length || events.length ? "" : `<div class="banner"><div class="copy"><h3>Coordinate your coding agents</h3>
      <p>Open Claude Code in this repository. Every session and subagent appears here, and Nodes gates each edit against the shared code graph.</p>
      <button class="btn primary" onclick="navigator.clipboard?.writeText('claude')">Copy: <code style="background:none;border:0">claude</code></button></div>
      <div class="art"><div class="win"><span class="label">Workspace</span><div style="margin-top:12px;display:flex;gap:10px">${["proceed", "defer", "handoff"].map(v =>
      `<span class="tag t-${VERDICT[v]}">${v}</span>`).join("")}</div></div></div></div>`;
  $("lanes").innerHTML = agents.map(a => `<div class="agent" style="--c:${color(a)}">
      <div class="top"><span class="who">${animalSVG(a, 30)}</span><span class="nm">${esc(nameOf(a))}</span>${state.done[a] ? '<span class="tag t-go">done</span>' : ""}
      <span class="task">${esc(laneTask[a] || "")}</span></div>
      <div class="chips">${[...(state.holds[a] || [])].map(u => `<span class="chip"><i></i>${esc(short(u))}</span>`).join("") || '<span class="muted" style="font-size:13px">holds nothing</span>'}</div>
      ${state.last[a] ? `<div class="last">${esc(state.last[a])}</div>` : ""}</div>`).join("")
    || `<div class="card empty"><b>No agents yet</b>Open Claude Code in this repository. Each session, and each subagent it starts, shows up here the moment it acts.</div>`;
  const items = state.feed.slice().reverse(), shown = showAll ? items : items.slice(0, 8);
  $("feed").innerHTML = shown.map((f, i) => `<div class="story ${f.cls}${i === 0 && fresh ? " new" : ""}" data-trace="${esc(f.e.trace || "")}">
      <span class="bar"></span><span>${esc(f.text)}</span><span class="when" title="${esc(clock(f.e.t))}">${esc(live ? ago(f.e.t) : clock(f.e.t))}</span></div>`).join("")
    || `<div class="empty"><b>Nothing has happened yet</b>Decisions, hand-offs and shared facts appear here in plain words as they happen. Click any line for the raw call.</div>`;
  $("more").hidden = items.length <= 8; $("more").textContent = showAll ? "Show less" : `Show all ${items.length}`;
}

function renderSpark() {  // calls per time slice as bars; each gate decision a dot in its verdict colour
  const at = events[(live ? events.length : cursor) - 1], replay = events.some(e => e.recorded_t);
  const cutoff = (!live || replay) && at ? (at.recorded_t ?? at.t) + .5 : Infinity;
  const cs = calls.filter(c => !quiet(c) && c.t <= cutoff), N = 60;
  $("spark-n").textContent = cs.length ? `${cs.length} calls` : "";
  if (!cs.length) { $("spark").innerHTML = `<text x="500" y="50" text-anchor="middle" fill="${T.ink2}" font-size="13">No calls yet. They appear here live.</text>`; return; }
  const t0 = Math.min(...cs.map(c => c.t)), t1 = Math.max(...cs.map(c => c.t), t0 + 1), w = 1000 / N;
  const bins = Array.from({length: N}, () => ({n: 0, v: []}));
  for (const c of cs) { const b = bins[Math.min(N - 1, ((c.t - t0) / (t1 - t0) * N) | 0)]; b.n++; if (c.verdict) b.v.push(c.verdict); }
  const max = Math.max(...bins.map(b => b.n));
  $("spark").innerHTML = bins.map((b, i) => { const h = b.n ? 6 + (b.n / max) * 58 : 2;
    return `<rect x="${i * w + 2}" y="${82 - h}" width="${w - 4}" height="${h}" rx="2" fill="${b.n ? T.spark : T.sparkOff}"><title>${b.n} calls</title></rect>`
      + b.v.slice(0, 3).map((v, j) => `<circle cx="${i * w + w / 2}" cy="${82 - h - 7 - j * 8}" r="3" fill="${VCOL[v]}"/>`).join(""); }).join("");
}

// ---------------------------------------------------------------- activity (MCP + hooks)
const effectsText = fx => { if (!fx) return '<span class="muted">not measured</span>'; const w = [];
  if (fx.nodes_created) w.push(`+${fx.nodes_created} node${fx.nodes_created > 1 ? "s" : ""}`);
  if (fx.relationships_created) w.push(`+${fx.relationships_created} rel${fx.relationships_created > 1 ? "s" : ""}`);
  if (fx.properties_set) w.push(`${fx.properties_set} set`);
  if (fx.nodes_deleted || fx.relationships_deleted) w.push(`−${(fx.nodes_deleted || 0) + (fx.relationships_deleted || 0)}`);
  return w.length ? `<span class="w">${w.join(" · ")}</span>` : `read · ${fx.rows || 0} rows`; };
const VERDICT = {proceed: "go", defer: "wait", escalate: "block", handoff: "hand"};
const VCOL = {proceed: "#4cc38a", defer: "#e8a93b", escalate: "#ef6461", handoff: "#5aa9f0"};
const writes = c => !!c.effects && ["nodes_created", "relationships_created", "properties_set", "nodes_deleted", "relationships_deleted"].some(k => c.effects[k]);
const quiet = c => !c.queries && !c.verdict && !c.error;  // a hook for a tool Nodes ignores (reads, searches)
const pat = p => { const m = p.match(/^(.+?)-\[:(\w+)\]->(.+)$/); return m ? `<span class="pat">${esc(m[1] === "?" ? "node" : m[1])} <i>→</i> ${esc(m[2])} <i>→</i> ${esc(m[3] === "?" ? "node" : m[3])}</span>` : ""; };

function renderCalls(newIds = new Set()) {
  // follow the timeline: replays and the slider show only the calls made up to the current event
  const at = events[(live ? events.length : cursor) - 1], replay = events.some(e => e.recorded_t);
  const cutoff = (!live || replay) && at ? (at.recorded_t ?? at.t) + .5 : Infinity;
  const inTime = calls.filter(c => c.t <= cutoff);
  const shown = inTime.filter(c => !quiet(c)), hidden = inTime.length - shown.length;
  const match = c => callFilter === "all" || (callFilter === "write" ? writes(c) : c.kind.startsWith(callFilter));
  const needle = ($("csearch").value || "").toLowerCase();
  const rows = shown.filter(match).filter(c => !needle || `${c.name} ${c.agent} ${c.verdict || ""}`.toLowerCase().includes(needle));
  const counts = {all: shown.length, tool: shown.filter(c => c.kind.startsWith("tool")).length,
                  hook: shown.filter(c => c.kind.startsWith("hook")).length, write: shown.filter(writes).length};
  const names = {all: "All", tool: "MCP tools", hook: "Hooks", write: "Wrote to graph"};
  document.querySelectorAll("#cfilter button").forEach(b => { b.textContent = `${names[b.dataset.f]} (${counts[b.dataset.f]})`; });
  $("calls").innerHTML = rows.slice(0, 120).map(c => `<tr data-id="${c.id}" class="${c.id === selCall ? "sel" : ""}${newIds.has(c.id) ? " new" : ""}">
      <td class="mono muted" title="${esc(new Date(c.t * 1000).toLocaleString())}">${esc(clock(c.t))}</td>
      <td><span style="color:${color(c.agent)}">●</span> ${esc(nameOf(c.agent) || "—")}</td>
      <td><span class="kind">${c.kind.startsWith("tool") ? "MCP" : c.kind.toUpperCase()}</span><span class="mono">${esc(c.name)}</span></td>
      <td>${c.error ? '<span class="tag t-block">error</span>' : c.verdict ? `<span class="tag t-${VERDICT[c.verdict] || "mute"}">${esc(c.verdict)}</span>` : '<span class="muted">—</span>'}</td>
      <td class="fx">${effectsText(c.effects)}</td><td class="num mono muted">${esc(c.ms)} ms</td></tr>`).join("");
  $("calls-empty").innerHTML = rows.length ? "" : `<div class="empty"><b>No calls ${calls.length ? "match this filter" : "traced yet"}</b>
      Every MCP tool call and Claude Code hook in this workspace is listed here live, with the Cypher it ran and what it changed in the graph.</div>`
    + (hidden ? `<div class="muted" style="font-size:12.5px;padding:10px 14px;border-top:1px solid var(--line)">${hidden} quiet hook call${hidden > 1 ? "s" : ""} hidden: tools Nodes doesn't gate (reads, searches), answered without touching the graph.</div>` : "");
  if (!selCall) $("calldetail").innerHTML = `<div class="empty" style="padding:8px 0"><b>Select a call</b>See its request, its response, the graph patterns it walked, and every Cypher query behind it, with timings.</div>`;
}

async function showCall(id) {
  selCall = id; renderCalls();
  const r = await (await fetch(`/api/trace?ws=${q(ws)}&id=${q(id)}`)).json();
  if (r.error) { $("calldetail").innerHTML = `<div class="empty"><b>Not traced</b>This event was recorded before raw-call tracing existed.</div>`; return; }
  const fx = (r.queries || []).some(x => x[3]) ? {} : null; (r.queries || []).forEach(x => Object.entries(x[3] || {}).forEach(([k, v]) => fx[k] = (fx[k] || 0) + v));
  const c = calls.find(x => x.id === id) || {patterns: []};
  $("calldetail").innerHTML = `<div class="label">${r.kind.startsWith("tool") ? "MCP tool" : "Hook"} · ${esc(clock(r.t))}</div>
    <h3 class="mono" style="margin-top:8px">${esc(r.name)}</h3><div class="muted" style="font-size:13px">${esc(nameOf(r.agent))} · ${esc(r.ms)} ms · ${(r.queries || []).length} queries</div>
    <div class="sect"><span class="label">Graph effects</span><div class="fx" style="margin-bottom:10px">${effectsText(fx)}</div>
      ${(c.patterns || []).map(pat).join("") || '<span class="muted" style="font-size:13px">No relationship walked.</span>'}</div>
    <div class="sect"><span class="label">Response</span><pre>${esc(JSON.stringify(r.response, null, 1))}</pre></div>
    <div class="sect"><span class="label">Request</span><details><summary>Show the request</summary><pre>${esc(JSON.stringify(r.request, null, 1))}</pre></details></div>
    <div class="sect"><span class="label">Cypher, in order</span>${(r.queries || []).map(x => `<details><summary><span class="mono">${esc(x[0])}</span>
      <span class="grow"></span><span class="fx">${x[3] ? effectsText(x[3]) : ""}</span><span class="mono muted">${esc(x[1])} ms</span></summary><pre>${esc(x[2])}</pre></details>`).join("")}</div>`;
}

let callsTimer = null, serverP50 = null;
function showLatency() {  // the gate's own p50 since the server started, else the median traced gate call
  const ms = calls.filter(c => c.verdict).map(c => c.ms).sort((a, b) => a - b);
  const v = serverP50 ?? (ms.length ? ms[ms.length >> 1] : null);
  $("k-lat").innerHTML = `${v ?? "–"}<small>ms</small>`; }
const refreshCalls = () => { clearTimeout(callsTimer); callsTimer = setTimeout(async () => {
  const before = new Set(calls.map(c => c.id));
  calls = await (await fetch(`/api/calls?ws=${q(ws)}`)).json(); showLatency(); renderSpark();
  renderCalls(before.size ? new Set(calls.filter(c => !before.has(c.id)).map(c => c.id)) : new Set()); }, 300); };

// ---------------------------------------------------------------- graph (FalkorDB canvas)
// The repo is the map (dim, tinted by kind); the code agents hold glows in their colour; leases stream particles;
// every gate decision sends a burst along the path it walked and a ripple through the nodes it touched.
const KIND = {file: "#7f95b5", function: "#4cc9a6", class: "#e9b44c", region: "#7b8390", planned: "#c4c8ce"};
const canvas = $("canvas");
let hover = null, ripples = {}, degree = {};
canvas.setConfig({
  backgroundColor: "rgba(0,0,0,0)", foregroundColor: T.ink, captionsKeys: ["name"], layoutMode: "force",
  nodeStyle: {fontFamily: "Inter, Segoe UI, system-ui, sans-serif"},
  linkStyle: {fontFamily: "Inter, Segoe UI, system-ui, sans-serif"},
  layoutOptions: {tree: {direction: "lr", levelDistance: 170, nodeSpacing: 46}},
  simulation: {chargeStrength: -520, centerStrength: .04},
  isNodeSelected: n => lit.has(n.id),
  node: {nodeCanvasObject: drawNode, nodePointerAreaPaint: (n, c, ctx) => { ctx.fillStyle = c; ctx.beginPath();
    ctx.arc(n.x, n.y, (n.size || 8) + 3, 0, 2 * Math.PI); ctx.fill(); }},
  linkLineDash: l => l.data?.by === "name-match" ? [3, 3] : null,
  eventHandlers: {onNodeClick: n => n.data?.raw ? showRawNode(n.data.raw) : showNode(n),
                  onBackgroundClick: () => source === "raw" ? renderRawSide() : renderGside(),
                  onNodeHover: n => { hover = n?.id || null; document.querySelector(".gstage").style.cursor = n ? "pointer" : ""; tip(n); }},
});
new ResizeObserver(([e]) => { const r = e.contentRect; if (r.width) { canvas.setWidth(r.width); canvas.setHeight(r.height); } }).observe(document.querySelector(".gstage"));
setInterval(() => canvas.getGraph?.()?.autoPauseRedraw(!$("v-graph").classList.contains("on")), 500);  // keep it moving

function drawNode(n, ctx) {  // FalkorDB's engine and layouts; our node face: kind colour, ownership ring, label
  const k = ctx.getTransform().a || 1, r = n.size || 6, agent = String(n.id).startsWith("agent:");
  const ring = n.data?.ring, on = lit.has(n.id), now = performance.now();
  const rip = ripples[n.id];  // a decision just walked through it: one ring that widens and fades
  if (rip) {
    const t = (now - rip.t) / 1200;
    if (t > 1) delete ripples[n.id];
    else { ctx.beginPath(); ctx.arc(n.x, n.y, r + (3 + 18 * t) / k, 0, 2 * Math.PI);
      ctx.strokeStyle = rip.c + Math.round((1 - t) * 230).toString(16).padStart(2, "0"); ctx.lineWidth = 1.5 / k; ctx.stroke(); }
  }
  ctx.beginPath();
  if (n.shape === "square") ctx.rect(n.x - r * .85, n.y - r * .85, r * 1.7, r * 1.7);
  else ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
  ctx.fillStyle = n.color; ctx.fill();
  ctx.lineWidth = 1 / k; ctx.strokeStyle = on || hover === n.id ? T.ink : T.stroke; ctx.stroke();
  if (ring) {  // who holds it
    ctx.beginPath(); ctx.arc(n.x, n.y, r + 3.5 / k, 0, 2 * Math.PI);
    ctx.setLineDash(n.data.done ? [3 / k, 2.5 / k] : []); ctx.lineWidth = 2.2 / k; ctx.strokeStyle = ring; ctx.stroke(); ctx.setLineDash([]);
  }
  if (agent) {
    ctx.fillStyle = "#121214"; ctx.font = `700 ${r * .95}px Inter, Segoe UI, sans-serif`; ctx.textAlign = "center";
    ctx.textBaseline = "middle"; ctx.fillText((n.data?.name || "?")[0].toUpperCase(), n.x, n.y + .5);
  }
  const label = n.data?.name || "", big = (degree[n.id] || 0) >= 4;
  const show = agent || ring || on || hover === n.id || (big && k > .9) || k > 1.6  // labels arrive as you zoom in
    || (n.data?.raw && (nodeCount < 30 || k > 1.3 || (degree[n.id] || 0) >= 4));
  if (!label || !show) return;
  ctx.font = `${agent || ring ? 600 : 400} ${(agent ? 12.5 : 11) / k}px Inter, Segoe UI, system-ui, sans-serif`;
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  ctx.fillStyle = agent || ring || on || hover === n.id || n.data?.raw ? T.ink : T.ink2;
  ctx.fillText(label.length > 28 ? label.slice(0, 27) + "…" : label, n.x, n.y + r + (ring ? 7 : 5) / k);
}

let owners = {}, worked = {};  // canonical unit id -> agent holding it now / who last worked on it
function nodeColor(id, n) {
  if (id.startsWith("agent:")) return color(id.slice(6));
  return KIND[n.planned ? "planned" : n.kind] || KIND.region;  // ownership is the ring, not the fill
}
function animateLinks() {  // particles stream from each agent into the code it holds
  const g = canvas.getGraph?.(); if (!g) return;
  g.linkDirectionalParticles(l => l.relationship === "HOLDS" ? 2 : 0).linkDirectionalParticleSpeed(.005)
   .linkDirectionalParticleWidth(l => l.relationship === "HOLDS" ? 2.2 : 2).linkDirectionalParticleColor(l => l.data?.burst || l.color);
}
let shape = "";
function renderGraph() {
  if (source === "raw") return;
  const agents = Object.keys(state.holds).filter(a => state.holds[a].size);
  // a planned unit (declared, or a new file the gate saw) is the same thing as the real file once it exists: fold it in
  const real = Object.fromEntries(graph.nodes.filter(n => n.kind === "file" && !n.id.includes("planned")).map(n => [n.id.split(":").slice(2).join(":"), n.id]));
  const planPath = id => (id.match(/^(?:file:)?planned:([^#]+)/) || [])[1];
  const canon = id => real[planPath(id)] || id;
  owners = {}; for (const a in state.holds) for (const u of state.holds[a]) owners[canon(u)] = a;
  worked = {}; for (const u in state.touched || {}) worked[canon(u)] = state.touched[u];
  const seen = new Set(), ids = new Set();
  const gnodes = graph.nodes.filter(n => { const c = canon(n.id); if (seen.has(c)) return false; seen.add(c); ids.add(c); return true; })
    .map(n => ({...n, id: canon(n.id), planned: canon(n.id) === n.id && !!planPath(n.id)}));
  const edges = graph.edges.filter(e => layout === "force" || e.rel !== "CALLS")
    .filter(e => canon(e.a) !== canon(e.b) && ids.has(canon(e.a)) && ids.has(canon(e.b)));
  degree = {}; for (const e of edges) { degree[canon(e.a)] = (degree[canon(e.a)] || 0) + 1; degree[canon(e.b)] = (degree[canon(e.b)] || 0) + 1; }
  const nodes = gnodes.map(n => ({id: n.id, labels: [n.kind], visible: true, color: nodeColor(n.id, n),
      size: owners[n.id] || worked[n.id] ? 11 : n.kind === "file" ? 8 : n.kind === "region" ? 6 : 7 + Math.min(4, Math.sqrt(degree[n.id] || 0) * 1.5),
      shape: n.kind === "file" ? "square" : "circle",
      data: {name: n.planned ? `${short(planPath(n.id))} (planned)` : short(n.id), kind: n.kind,
        ring: owners[n.id] || worked[n.id] ? color(owners[n.id] || worked[n.id]) : null, done: !owners[n.id] && !!worked[n.id]}}))
    .concat(agents.map(a => ({id: `agent:${a}`, labels: ["Session"], visible: true, color: color(a), size: 16, data: {name: nameOf(a)}})));
  const links = edges.map((e, i) => ({id: `e${i}`, relationship: "", source: canon(e.a), target: canon(e.b), visible: true,
      color: e.rel === "CALLS" ? T.calls : T.contains, data: {by: e.by, rel: e.rel}}))  // structure: lines, no captions
    .concat(agents.flatMap(a => [...new Set([...state.holds[a]].map(canon))].filter(u => ids.has(u)).map(u => ({id: `h:${a}:${u}`,
      relationship: "HOLDS", source: `agent:${a}`, target: u, visible: true, color: color(a), width: 1.6, data: {}}))));
  nodeCount = nodes.length;
  const sig = nodes.map(n => n.id).join("|") + "#" + links.map(l => l.id).join("|") + layout;
  if (sig !== shape) { shape = sig; canvas.setData({nodes, links}); setTimeout(animateLinks, 50); }
  else {
    const cur = Object.fromEntries(nodes.map(n => [n.id, n]));
    (canvas.getGraphData()?.nodes || []).forEach(n => { const c = cur[n.id]; if (c) { n.color = c.color; n.size = c.size; n.data = c.data; } });
    canvas.refresh();
  }
  $("gempty").hidden = nodes.length > 0;
  $("gempty").innerHTML = `<div class="empty" style="max-width:380px"><b>No code indexed yet</b>Run <code>nodes init</code> in a repository. Its files and functions appear here as a map, and the code agents hold lights up in their colour.</div>`;
  const people = [...new Set([...agents, ...Object.values(worked)])];
  $("legend").innerHTML = people.map(a => `<span><i style="background:transparent;border:2px ${agents.includes(a) ? "solid" : "dashed"} ${color(a)};width:11px;height:11px"></i>${esc(nameOf(a))}${agents.includes(a) ? "" : ' <span class="muted">(done)</span>'}</span>`).join("")
    + `<span><i style="background:${KIND.function}"></i>function</span><span><i style="background:${KIND.class}"></i>class</span>`
    + `<span><i style="background:${KIND.file};border-radius:2px"></i>file</span><span><i style="background:${KIND.region}"></i>block</span>`
    + (graph.scope === "active" ? `<span class="muted">active area of ${graph.total} units</span>` : "");
}
function light(ids, ms = 2600, c = T.ink) {  // a traversal: ripple the nodes it touched, burst along their links
  lit = new Set(ids); const now = performance.now(); ids.forEach(id => { ripples[id] = {t: now, c}; });
  const g = canvas.getGraph?.(), set = new Set(ids);
  if (g) for (const l of canvas.getGraphData()?.links || []) {
    const s = l.source?.id ?? l.source, t = l.target?.id ?? l.target;
    if (set.has(t) && (set.has(s) || l.data?.rel !== "CALLS")) {
      l.data = {...l.data, burst: c};
      for (let i = 0; i < 4; i++) setTimeout(() => g.emitParticle(l), i * 120);
    }
  }
  canvas.refresh(); clearTimeout(litTimer);
  litTimer = setTimeout(() => { lit = new Set(); (canvas.getGraphData()?.links || []).forEach(l => { if (l.data?.burst) delete l.data.burst; }); canvas.refresh(); }, ms);
}
const pathIds = e => [`agent:${e.agent}`, ...((e.path || "").match(UNIT_RE) || []), ...(e.units || [])];

function travText(e) {  // "alice -DONE_AT-> 0fe00a8… -HANDS-> order_total -TO-> bob", with readable names
  return (e.path || "").split(" ").map(t => /^-[A-Z_]+->$/.test(t) ? `<span class="muted mono" style="font-size:11px">${esc(t.slice(1, -2))}</span>`
    : /^[0-9a-f]{40}$/.test(t) ? `<span class="mono">${t.slice(0, 7)}</span>` : /^(fn|region|file|class):/.test(t) ? `<b style="font-weight:500">${esc(short(t.replace(/,$/, "")))}</b>`
    : esc(nameOf(t))).join(" ");
}

function renderGside() {
  const recent = events.slice(0, live ? events.length : cursor).filter(e => e.kind === "decision" || e.kind === "query").slice(-8).reverse();
  $("gside").innerHTML = `<span class="label">Recent traversals</span>
    <p class="muted" style="font-size:13px;margin:8px 0 14px">Each gate decision walks the graph from an agent to the code it wants. Click one to light its path.</p>
    ${recent.map((e, i) => `<div class="story ${VERDICT[e.verdict] || ""}" data-i="${events.indexOf(e)}" style="padding:10px 0;grid-template-columns:10px 1fr"><span class="bar"></span>
      <span style="font-size:13px;line-height:1.7">${e.kind === "query" ? `<b style="font-weight:500">${esc(nameOf(e.agent))}</b> <span class="muted mono" style="font-size:11px">${esc(e.tool)}</span> ${esc((e.summary || "").replace(/^[a-z_]+: /, ""))}`
        : (e.advisory ? '<span class="tag t-wait" style="margin-right:6px">advisory</span>' : "") + travText(e)}</span></div>`).join("")
      || '<div class="muted" style="font-size:13px">No decisions yet.</div>'}
    <div class="sect"><span class="label">Node</span><p class="muted" style="font-size:13px;margin:8px 0 0">Click a node for its lease, authors, last change and the facts on it.</p></div>`;
}
async function showNode(n) {
  if (String(n.id).startsWith("agent:")) { const a = n.id.slice(6); $("gside").innerHTML = `<span class="label">Agent</span><h3 style="margin:8px 0 4px">${esc(nameOf(a))}</h3>
      <div class="chips">${[...(state.holds[a] || [])].map(u => `<span class="chip">${esc(short(u))}</span>`).join("")}</div><div class="last" style="margin-top:10px;color:var(--ink2)">${esc(state.last[a] || "")}</div>`; return; }
  const d = await (await fetch(`/api/node?ws=${q(ws)}&uid=${q(n.id)}`)).json();
  if (d.error) return;
  const row = (k, v) => v ? `<div class="kv"><span>${k}</span><b title="${esc(v)}">${esc(v)}</b></div>` : "";
  const list = (title, items) => items.length ? `<div class="sect"><span class="label">${title}</span>${items.join("")}</div>` : "";
  $("gside").innerHTML = `<span class="label">${esc(d.kind)}</span><h3 class="mono" style="margin:8px 0 2px;word-break:break-all">${esc(d.symbol || d.path)}</h3>
    <div class="muted mono" style="font-size:12px">${esc(d.path)}${d.lines?.[0] ? `:${d.lines[0]}-${d.lines[1]}` : ""}</div>
    <div class="sect">${row("Lease", d.lease ? `${nameOf(d.lease.owner)} · epoch ${d.lease.epoch}` : "free")}${row("Language", d.lang)}${row("Status", d.status)}</div>
    ${d.last_change ? `<div class="sect"><span class="label">Last change</span><div class="mono muted" style="font-size:12px">${esc(String(d.last_change.commit).slice(0, 10))}</div><div style="font-size:13px;margin-top:4px;color:var(--ink2)">${esc(d.last_change.why)}</div></div>` : ""}
    ${list("Written by", d.authors.map(a => `<div class="kv"><span>${esc(a.name || a.id)}</span><b>${a.lines} lines</b></div>`))}
    ${list("Lease history", d.lease_history.map(h => `<div class="kv"><span>${esc(nameOf(h.agent))}</span><b>epoch ${h.epoch}</b></div>`))}
    ${list("Decisions", d.decisions.map(x => `<div style="font-size:13px;padding:5px 0"><span class="tag t-${VERDICT[x.verdict] || "mute"}">${esc(x.verdict)}</span> <span class="muted">${esc(nameOf(x.agent))}</span></div>`))}
    ${list("Calls", [`<div class="chips">${d.calls.map(c => `<span class="chip">${esc(c)}</span>`).join("")}</div>`].filter(() => d.calls.length))}
    ${list("Called by", [`<div class="chips">${d.called_by.map(c => `<span class="chip">${esc(c)}</span>`).join("")}</div>`].filter(() => d.called_by.length))}
    ${list("Facts", d.facts.map(f => `<div style="font-size:13px;padding:6px 0"><span class="tag ${f.status === "source-checked" ? "t-go" : "t-mute"}">${esc(f.status)}</span> ${esc(f.text)}</div>`))}`;
  light([n.id], 1800);
}

// ---------------------------------------------------------------- raw graph: any graph in this FalkorDB, Browser-style
let source = "code", rawData = null, rawOff = {labels: new Set(), rels: new Set()}, nodeCount = 0, mouse = {x: 0, y: 0};
const LABEL_COLORS = ["#4cc9a6", "#ff6b6b", "#4dabf7", "#ffd43b", "#ff922b", "#3bc9db", "#94d82d", "#f783ac", "#e9ecef", "#c9a27e", "#74c0fc", "#ffa8a8"];
const labelColors = {};
const lcol = l => labelColors[l] ??= LABEL_COLORS[Object.keys(labelColors).length % LABEL_COLORS.length];
const CAPTION = ["name", "symbol", "title", "verdict", "text", "path", "sha", "uid", "id"];
const caption = p => { for (const k of CAPTION) if (p[k] != null && p[k] !== "") return String(p[k]); return ""; };
const shortCap = s => { const t = s.includes("#") ? s.split("#").pop() : s.split("/").pop() || s; return t.length > 30 ? t.slice(0, 29) + "…" : t; };
const val = v => typeof v === "object" ? JSON.stringify(v) : String(v);

async function loadGraphList() {
  const names = await (await fetch("/api/graphs")).json();
  const pick = $("rawsel").value || `ws:${ws}`;
  $("rawsel").innerHTML = names.map(n => `<option ${n === pick ? "selected" : ""}>${esc(n)}</option>`).join("");
}
async function loadRaw() {
  rawData = await (await fetch(`/api/raw?graph=${q($("rawsel").value)}&limit=600`)).json();
  if (rawData.error) { rawData = null; return; }
  rawOff = {labels: new Set(), rels: new Set()}; renderRaw(); renderRawSide();
}
function renderRaw() {
  const d = rawData; if (!d) return;
  const nodes = d.nodes.filter(n => !rawOff.labels.has(n.labels[0])).map(n => ({id: `r${n.id}`, labels: n.labels, visible: true,
    color: lcol(n.labels[0]), size: 7, data: {name: shortCap(caption(n.props)), raw: n}}));
  const ids = new Set(nodes.map(n => n.id));
  const links = d.edges.filter(e => !rawOff.rels.has(e.type) && ids.has(`r${e.src}`) && ids.has(`r${e.dst}`))
    .map(e => ({id: `re${e.id}`, relationship: e.type, source: `r${e.src}`, target: `r${e.dst}`, visible: true, color: T.raw, data: {raw: e}}));
  degree = {}; links.forEach(l => { degree[l.source] = (degree[l.source] || 0) + 1; degree[l.target] = (degree[l.target] || 0) + 1; });
  nodes.forEach(n => { n.size = 6 + Math.min(6, Math.sqrt(degree[n.id] || 0) * 1.5); });
  nodeCount = nodes.length; shape = "raw"; canvas.setData({nodes, links}); setTimeout(animateLinks, 50);
  $("gempty").hidden = nodes.length > 0;
  $("gempty").innerHTML = `<div class="empty"><b>Nothing to show</b>Turn a label back on, or pick another graph.</div>`;
  $("gpanel").innerHTML = `<div class="box"><span class="label">Labels</span>${Object.entries(d.labels).filter(([, c]) => c).map(([l, c]) =>
      `<button class="toggle ${rawOff.labels.has(l) ? "off" : ""}" data-label="${esc(l)}"><i style="background:${lcol(l)}"></i>${esc(l)}<small>${c}</small></button>`).join("")
      || '<span class="muted" style="font-size:12.5px">empty graph</span>'}</div>
    <div class="box"><span class="label">Relationships</span>${Object.entries(d.relationships).filter(([, c]) => c).map(([t, c]) =>
      `<button class="toggle rel ${rawOff.rels.has(t) ? "off" : ""}" data-rel="${esc(t)}"><i style="background:#8a97a3"></i>${esc(t)}<small>${c}</small></button>`).join("")
      || '<span class="muted" style="font-size:12.5px">none</span>'}</div>`;
  $("legend").innerHTML = `<span class="muted">${nodes.length} of ${d.total.toLocaleString()} nodes · ${links.length} relationships shown</span>`;
}
function renderRawSide() {
  $("gside").innerHTML = `<span class="label">Graph</span><h3 class="mono" style="margin:8px 0 4px;word-break:break-all">${esc(rawData?.graph || "")}</h3>
    <p class="muted" style="font-size:13px;margin:0 0 6px">Read-only. Up to 600 nodes, shared fairly across the labels you have on.</p>
    <div class="sect"><span class="label">Node</span><p class="muted" style="font-size:13px;margin:8px 0 0">Hover a node for a summary; click it for every property.</p></div>`;
}
function showRawNode(r) {
  $("gside").innerHTML = `<div style="display:flex;gap:6px;flex-wrap:wrap">${r.labels.map(l => `<span class="tag" style="background:${lcol(l)};color:#111">${esc(l)}</span>`).join("")}</div>
    <h3 style="margin:10px 0 12px;word-break:break-all">${esc(caption(r.props) || `#${r.id}`)}</h3>
    <div class="props"><div class="row"><span>&lt;id&gt;</span><span class="mono">${r.id}</span></div>
    ${Object.entries(r.props).map(([k, v]) => `<div class="row"><span>${esc(k)}</span><span>${esc(val(v))}</span></div>`).join("")}</div>`;
}
function placeTip() {
  const t = $("gtip"); if (t.hidden) return; const box = t.parentElement;
  t.style.left = Math.max(8, Math.min(mouse.x + 16, box.clientWidth - t.offsetWidth - 8)) + "px";
  t.style.top = Math.max(8, Math.min(mouse.y + 16, box.clientHeight - t.offsetHeight - 8)) + "px";
}
function tip(n) {  // what a node holds, on hover
  const t = $("gtip"); if (!n) { t.hidden = true; return; }
  const row = (k, v) => `<div class="row"><span>${esc(k)}</span><span>${esc(String(v).slice(0, 96))}</span></div>`;
  let html;
  if (n.data?.raw) {
    const r = n.data.raw;
    html = `<div class="lbls">${r.labels.map(l => `<span class="lbl" style="background:${lcol(l)}">${esc(l)}</span>`).join("")}</div>`
      + Object.entries(r.props).slice(0, 8).map(([k, v]) => row(k, val(v))).join("")
      + (Object.keys(r.props).length > 8 ? `<div class="muted" style="margin-top:6px">+${Object.keys(r.props).length - 8} more: click</div>` : "");
  } else if (String(n.id).startsWith("agent:")) {
    const a = n.id.slice(6);
    html = `<div class="lbls"><span class="lbl" style="background:${n.color}">agent</span></div>${row("name", nameOf(a))}`
      + row("holds", `${state.holds[a]?.size || 0} unit(s)`) + (state.last[a] ? row("last", state.last[a]) : "");
  } else {
    const own = owners[n.id], w = worked[n.id];
    html = `<div class="lbls"><span class="lbl" style="background:${n.color}">${esc(n.data?.kind || "unit")}</span></div>`
      + row("name", n.data?.name || "") + row("uid", n.id) + (own ? row("held by", nameOf(own)) : w ? row("worked on by", nameOf(w)) : "")
      + row("links", degree[n.id] || 0) + `<div class="muted" style="margin-top:6px">Click for lease, authors and history</div>`;
  }
  t.innerHTML = html; t.hidden = false; placeTip();
}
document.querySelector(".gstage").addEventListener("mousemove", e => {
  const r = e.currentTarget.getBoundingClientRect(); mouse = {x: e.clientX - r.left, y: e.clientY - r.top}; placeTip(); });
document.querySelector(".gstage").addEventListener("mouseleave", () => { $("gtip").hidden = true; });
$("gpanel").onclick = e => { const b = e.target.closest(".toggle"); if (!b) return;
  const set = b.dataset.label ? rawOff.labels : rawOff.rels, key = b.dataset.label || b.dataset.rel;
  set.has(key) ? set.delete(key) : set.add(key); renderRaw(); };
$("rawsel").onchange = () => loadRaw();
$("gsource").onclick = async e => { const b = e.target.closest("button"); if (!b || b.dataset.s === source) return; source = b.dataset.s;
  document.querySelectorAll("#gsource button").forEach(x => x.classList.toggle("on", x === b));
  $("rawpick").hidden = $("gpanel").hidden = source !== "raw"; $("gtip").hidden = true; shape = "";
  $("gsub").textContent = source === "raw" ? "Any graph in this FalkorDB, as stored. Toggle labels and relationships; hover or click a node to see what it holds."
    : "Your code as FalkorDB stores it, coloured by kind. A ring shows which agent holds it.";
  if (source === "raw") { await loadGraphList(); await loadRaw(); } else { renderGraph(); renderGside(); } };

// ---------------------------------------------------------------- live view: agents at work on a floor plan
const liveView = new Live($("live"), {esc, color, nameOf, short, animalSVG});
let liveApplied = -1;  // how many events the scene reflects; -1 = not built yet
function renderLive() {
  if (!$("v-live").classList.contains("on")) return;
  const n = live ? events.length : cursor;
  if (liveView.graphSize !== graph.nodes.length) liveApplied = -1;                       // the map changed: rebuild
  if (liveApplied >= 0 && n === liveApplied + 1) liveView.apply(events[n - 1], true);  // one step: animate it
  else if (n !== liveApplied) liveView.replay(graph, events.slice(0, n));            // a jump: rebuild, no animation
  liveApplied = n;
}
$("theme").onclick = () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try { localStorage.setItem("nodes-theme", dark ? "dark" : "light"); } catch { /* private window: per-visit only */ }
  readTheme(); canvas.setConfig({foregroundColor: T.ink}); shape = "";
  source === "raw" ? renderRaw() : renderGraph(); renderSpark();
};
$("lvfull").onclick = () => document.fullscreenElement ? document.exitFullscreen() : $("live").requestFullscreen?.();

// ---------------------------------------------------------------- facts
function renderFacts() {
  $("facts").innerHTML = facts.map(f => `<div class="fact"><div class="meta"><span class="tag ${f.status === "source-checked" || f.status === "observed" ? "t-go" : f.status === "extracted" ? "t-hand" : "t-block"}">${esc(f.status === "unverified" ? "unverified" : f.status)}</span>
      <span>${esc(nameOf(f.by))}</span><span class="mono">${esc(f.source)}</span></div><div>${esc(f.text)}</div></div>`).join("")
    || `<div class="empty"><b>No facts shared yet</b>When an agent tells the team something with <code>post_fact</code>, it lands here with its trust level: <b style="display:inline;color:var(--go)">source-checked</b> when Nodes found it in the cited code, unverified when it could not.</div>`;
}

// ---------------------------------------------------------------- server panel
async function refreshServer() {
  try {
    const s = await (await fetch(`/api/server?ws=${q(ws)}`)).json();
    $("srvdot").classList.add("on"); $("srv-status").textContent = "running";
    $("srv-url").textContent = s.server.mcp.replace(/^https?:\/\//, ""); $("srv-url").title = s.server.mcp;
    const u = s.server.uptime_s; $("srv-up").textContent = u < 3600 ? `${u / 60 | 0}m` : `${u / 3600 | 0}h ${(u % 3600) / 60 | 0}m`;
    $("srv-tools").textContent = `${s.server.tools.length} · ${s.server.hooks.length} hooks`; $("srv-tools").title = s.server.tools.join(", ");
    $("db-ver").textContent = s.falkordb.module_version || "–"; $("db-graph").textContent = s.falkordb.graph || "–";
    const c = s.workspace.counts || {}, n = Object.entries(c).filter(([k]) => k !== "_edges").reduce((a, [, v]) => a + v, 0);
    $("db-size").textContent = c._edges != null ? `${n.toLocaleString()} · ${c._edges.toLocaleString()}` : "–";
    $("db-ping").textContent = `${s.falkordb.ping_ms} ms`; $("ws-root").textContent = s.workspace.root || " ";
    $("browser").hidden = !s.falkordb.browser; $("browser").href = s.falkordb.browser;
    $("browser-hint").innerHTML = s.falkordb.browser ? `Log in with <b>${esc(s.falkordb.browser_login)}</b>, then pick <b>${esc(s.falkordb.graph)}</b>`
      : "FalkorDB Browser isn't running. A fresh <code>nodes up</code> serves it on :3001.";
  } catch { $("srvdot").classList.remove("on"); $("srv-status").textContent = "unreachable"; }
}

// ---------------------------------------------------------------- data flow
async function refreshState() {
  try {
    const st = await (await fetch(`/api/state?ws=${q(ws)}`)).json();
    laneTask = Object.fromEntries((st.sessions || []).map(x => [x.name, x.task]));
    $("ws").innerHTML = (st.workspaces || []).map(w => `<option ${w === ws ? "selected" : ""}>${esc(w)}</option>`).join("");
    const g = (st.latency || {})["gate.lease"]; serverP50 = g ? g.p50_ms : null; showLatency();
    facts = st.facts || []; renderFacts();
    graph = await (await fetch(`/api/graph?ws=${q(ws)}`)).json();
  } catch { /* server restarting */ }
}
function render(fresh = false) {
  state = rebuild(live ? events.length : cursor);
  renderOverview(fresh); renderSpark(); renderGraph(); renderLive(); if (source === "code" && !document.querySelector("#gside .kv, #gside .props")) renderGside();
  $("mode").textContent = live ? "live" : `${cursor} / ${events.length}`;
  $("dot").classList.toggle("on", live && !!es);
}
async function connect() {
  if (es) es.close();
  liveApplied = -1;
  $("crumb-ws").textContent = ws || "–"; $("ws-avatar").textContent = (ws[0] || "N").toUpperCase(); shape = ""; selCall = null;
  if (source === "raw") { $("rawsel").value = `ws:${ws}`; loadGraphList().then(loadRaw); }
  events = await (await fetch(`/api/events?ws=${q(ws)}`)).json();
  $("slider").max = events.length; $("slider").value = cursor = events.length; live = true;
  await refreshState(); render(); refreshServer(); refreshCalls();
  es = new EventSource(`/api/stream?ws=${q(ws)}&from=${q(events.length ? events[events.length - 1].id : "$")}`);
  es.onopen = () => render();
  es.onerror = () => { $("dot").classList.remove("on"); $("mode").textContent = "reconnecting"; };
  es.onmessage = async m => { const e = JSON.parse(m.data); events.push(e); $("slider").max = events.length; refreshCalls();
    if (!live) return;
    $("slider").value = cursor = events.length; await refreshState(); render(true); renderCalls();
    if (e.kind === "decision") light(pathIds(e), 2600, e.advisory ? "#e8a93b" : VCOL[e.verdict]);
    if (e.kind === "query") light(pathIds(e), 2600, T.ink); };
}

// ---------------------------------------------------------------- controls
$("nav").onclick = ev => { const b = ev.target.closest("button"); if (!b) return;
  document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.id === `v-${b.dataset.view}`));
  $("crumb-view").textContent = b.textContent.trim(); if (b.dataset.view === "live") { liveApplied = -1; setTimeout(renderLive, 30); } if (b.dataset.view === "graph") setTimeout(() => canvas.zoomToFit?.(.85), 60); };
$("ws").onchange = e => { ws = e.target.value; history.replaceState(null, "", `?ws=${q(ws)}`); connect(); };
$("more").onclick = () => { showAll = !showAll; renderOverview(); };
$("cfilter").onclick = e => { const b = e.target.closest("button"); if (!b) return; callFilter = b.dataset.f;
  document.querySelectorAll("#cfilter button").forEach(x => x.classList.toggle("on", x === b)); renderCalls(); };
$("csearch").oninput = () => renderCalls();
$("calls").onclick = e => { const tr = e.target.closest("tr"); if (tr) showCall(tr.dataset.id); };
$("feed").onclick = e => { const el = e.target.closest(".story"); if (!el?.dataset.trace) return;
  document.querySelector('#nav [data-view="activity"]').click(); showCall(el.dataset.trace); };
$("gside").onclick = e => { const el = e.target.closest("[data-i]"); if (el) { const ev = events[+el.dataset.i]; light(pathIds(ev), 2600, VCOL[ev.verdict]); } };
$("layout").onclick = e => { const b = e.target.closest("button"); if (!b) return; layout = b.dataset.l;
  document.querySelectorAll("#layout button").forEach(x => x.classList.toggle("on", x === b));
  canvas.setLayout(layout); shape = ""; renderGraph(); };
$("fit").onclick = () => canvas.zoomToFit(.85);
$("slider").oninput = e => { cursor = +e.target.value; live = cursor >= events.length; stopPlay(); render(); renderCalls(); };
$("livepill").onclick = () => { live = true; cursor = events.length; $("slider").value = cursor; stopPlay(); render(); };
const stopPlay = () => { if (playing) clearInterval(playing); playing = null; $("play").textContent = "Replay"; };
$("play").onclick = () => { if (playing) return stopPlay();
  live = false; cursor = 0; $("play").textContent = "Pause";
  playing = setInterval(() => { if (cursor >= events.length) { stopPlay(); live = true; render(); return; }
    cursor++; $("slider").value = cursor; render(true); const e = events[cursor - 1]; if (e.kind === "decision") light(pathIds(e), 850, VCOL[e.verdict]); }, 900); };
setInterval(refreshServer, 10000);
setInterval(() => live && renderOverview(), 30000);  // keep "2m ago" honest
(async () => { if (!ws) { const st = await (await fetch("/api/state")).json(); ws = st.workspace || ""; } connect(); })();
