// The Live view: the repository as a floor plan (files are rooms, functions and classes are desks) and every agent
// as an avatar that walks to the code it works on. It is driven only by the workspace's event stream, so a recorded
// run replays exactly like a live one.
const W = 1200, H = 720, LOBBY_W = 178, TOP = 58, PAD = 14;
const VCOL = {proceed: "#4cc38a", defer: "#e8a93b", escalate: "#ef6461", handoff: "#5aa9f0", acknowledged: "#4cc38a"};
const UNIT = /(fn|region|file|class):[^ ,]+/g;
const pathOf = uid => String(uid).split(":").slice(2).join(":").split("#")[0];

export class Live {
  constructor(root, helpers) {
    this.root = root;
    Object.assign(this, helpers);  // esc, color, nameOf, short
    root.innerHTML = `<div class="lv-stage"><svg class="lv-lines" viewBox="0 0 ${W} ${H}"></svg>
      <div class="lv-lobby"><span class="label">Lobby</span></div><div class="lv-rooms"></div><div class="lv-agents"></div>
      <div class="lv-fx"></div><div class="lv-caption"></div></div>`;
    this.stage = root.querySelector(".lv-stage");
    this.lines = root.querySelector(".lv-lines");
    new ResizeObserver(() => this.fit()).observe(root);
    this.agents = {};
    this.desks = {};
  }

  fit() {
    const s = Math.min(this.root.clientWidth / W, this.root.clientHeight / H) || 1;
    this.stage.style.transform = `scale(${s})`;
    this.root.style.setProperty("--lv-h", `${H * s}px`);
  }

  // ------------------------------------------------------------------ the floor plan
  build(graph) {
    this.graphSize = graph.nodes.length;
    const rooms = {};
    for (const n of graph.nodes) {
      if (n.kind !== "function" && n.kind !== "class") continue;
      const p = pathOf(n.id);
      if (!p || p.startsWith("planned")) continue;
      (rooms[p] ??= []).push(n);
    }
    let files = Object.keys(rooms).sort();
    if (files.length > 24) files = files.sort((a, b) => rooms[b].length - rooms[a].length).slice(0, 24).sort();
    const n = Math.max(1, files.length), cols = Math.ceil(Math.sqrt(n * 1.7)), rows = Math.ceil(n / cols);
    const x0 = LOBBY_W + 30, w = (W - x0 - 20 - (cols - 1) * PAD) / cols, h = (H - TOP - 20 - (rows - 1) * PAD) / rows;
    this.desks = {};
    this.roomAt = {};
    this.owner = {};
    const html = files.map((f, i) => {
      const x = x0 + (i % cols) * (w + PAD), y = TOP + Math.floor(i / cols) * (h + PAD);
      this.roomAt[f] = {x: x + w / 2, y: y + h / 2};
      const units = rooms[f].sort((a, b) => a.id.localeCompare(b.id)).slice(0, 40);
      const dc = Math.max(3, Math.ceil(Math.sqrt(units.length * w / h))), dw = (w - 24) / dc;
      const desks = units.map((u, k) => {
        const dx = x + 10 + (k % dc) * dw + dw / 2, dy = y + 38 + Math.floor(k / dc) * 24 + 8;
        this.desks[u.id] = {x: dx, y: dy, room: f};
        return `<div class="lv-desk lv-${u.kind}" data-uid="${this.esc(u.id)}" style="left:${dx - 6}px;top:${dy - 6}px"
          title="${this.esc(this.short(u.id))}"></div><div class="lv-dlabel" data-for="${this.esc(u.id)}" style="left:${dx}px;top:${dy + 9}px">${this.esc(this.short(u.id))}</div>`;
      }).join("");
      return `<div class="lv-room" style="left:${x}px;top:${y}px;width:${w}px;height:${h}px"><span class="lv-rname">${this.esc(f)}</span>
        ${rooms[f].length > 40 ? `<span class="lv-more">+${rooms[f].length - 40}</span>` : ""}</div>${desks}`;
    }).join("");
    this.root.querySelector(".lv-rooms").innerHTML = html || `<div class="lv-empty">No code indexed in this workspace yet.</div>`;
    this.root.querySelector(".lv-agents").innerHTML = "";
    this.root.querySelector(".lv-fx").innerHTML = "";
    this.lines.innerHTML = "";
    this.agents = {};
    this.fit();
  }

  // ------------------------------------------------------------------ actors
  agent(name) {
    if (this.agents[name]) return this.agents[name];
    const slot = Object.keys(this.agents).length, c = this.color(name);
    const el = document.createElement("div");
    el.className = "lv-agent";
    el.style.setProperty("--c", c);
    el.innerHTML = `<div class="lv-bubble"></div><div class="lv-avatar">${this.animalSVG(name, 46)}</div>
      <div class="lv-name">${this.esc(this.nameOf(name))}</div><div class="lv-task"></div>`;
    this.root.querySelector(".lv-agents").appendChild(el);
    const a = {el, name, home: {x: 52 + Math.floor(slot / 6) * 92, y: TOP + 62 + (slot % 6) * 104}, at: null, done: false};
    this.agents[name] = a;
    this.move(a, a.home, false);
    return a;
  }
  move(a, p, animate = true) {
    a.el.classList.toggle("lv-instant", !animate);
    a.at = p;
    a.el.style.transform = `translate(${p.x - 22}px, ${p.y - 22}px)`;
  }
  near(uid, a) {  // stand beside a desk, a little offset per agent so two agents never sit on one spot
    const d = this.desks[uid] || this.roomAt[pathOf(uid)];
    if (!d) return null;
    const k = Object.keys(this.agents).indexOf(a.name);
    return {x: d.x + 26 + (k % 3) * 8, y: d.y - 30 - (k % 2) * 10};
  }
  say(a, text, tone = "", animate = true) {
    const b = a.el.querySelector(".lv-bubble");
    b.textContent = text;
    b.className = `lv-bubble ${animate ? "show" : ""} ${tone}`;  // a silent rebuild shows no bubbles
    if (animate) { clearTimeout(a.sayT); a.sayT = setTimeout(() => b.classList.remove("show"), 4200); }
  }
  own(uid, name, how = "") {  // a desk taken by an agent (solid ring), planned (dashed) or freed
    const el = this.root.querySelector(`.lv-desk[data-uid="${CSS.escape(uid)}"]`);
    const lbl = this.root.querySelector(`.lv-dlabel[data-for="${CSS.escape(uid)}"]`);
    if (!el) return;
    el.style.setProperty("--c", name ? this.color(name) : "transparent");
    el.dataset.state = name ? how || "held" : "";
    if (name) this.owner[uid] = name; else delete this.owner[uid];
    lbl?.classList.toggle("show", !!name);
  }
  pulse(uid) {
    const el = this.root.querySelector(`.lv-desk[data-uid="${CSS.escape(uid)}"]`);
    if (el) { el.classList.remove("lv-pulse"); void el.offsetWidth; el.classList.add("lv-pulse"); }
  }
  line(from, to, color, {dashed = false, ttl = 2200, cls = ""} = {}) {
    if (!from || !to) return;
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const mx = (from.x + to.x) / 2, my = Math.min(from.y, to.y) - 40;
    p.setAttribute("d", `M${from.x},${from.y} Q${mx},${my} ${to.x},${to.y}`);
    p.setAttribute("stroke", color);
    p.setAttribute("class", `lv-ray ${dashed ? "dashed" : ""} ${cls}`);
    this.lines.appendChild(p);
    if (ttl) setTimeout(() => p.remove(), ttl);
    return p;
  }
  card(a, text, tone) {
    const el = document.createElement("div");
    el.className = `lv-card ${tone}`;
    el.textContent = text;
    el.style.left = `${a.at.x - 70}px`;
    el.style.top = `${a.at.y - 60}px`;
    this.root.querySelector(".lv-fx").appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }
  baton(from, to, color) {
    if (!from || !to) return;
    const el = document.createElement("div");
    el.className = "lv-baton";
    el.style.background = color;
    el.style.transform = `translate(${from.x}px, ${from.y}px)`;
    this.root.querySelector(".lv-fx").appendChild(el);
    requestAnimationFrame(() => requestAnimationFrame(() => { el.style.transform = `translate(${to.x}px, ${to.y}px)`; }));
    setTimeout(() => el.remove(), 1500);
  }
  caption(text) { this.root.querySelector(".lv-caption").textContent = text; }

  // ------------------------------------------------------------------ events -> the scene
  apply(e, animate = true) {
    if (!e.agent || e.agent === "importer" || e.agent === "git history") return;
    const a = this.agent(e.agent), p = (e.path || "").split(" "), units = (e.path || "").match(UNIT) || [];
    const short = u => this.short(u), who = n => this.nameOf(n);
    if (a.done && !["subagent-stop", "session-end", "done"].includes(e.kind)) { a.done = false; a.el.classList.remove("lv-done"); }
    const fx = animate ? this : null;
    switch (e.kind) {
      case "join": case "subagent-start":
        a.el.querySelector(".lv-task").textContent = e.task || "";
        this.say(a, e.task ? `on it: ${e.task}` : "joined", "", animate);
        break;
      case "intent":
        (e.units || []).forEach(u => this.own(u, e.agent, "planned"));
        this.say(a, `planning ${(e.units || []).slice(0, 2).map(short).join(", ") || "its change"}`, "", animate);
        break;
      case "query": {
        const [src, ...hits] = e.units || [];
        if (src && this.near(src, a)) this.move(a, this.near(src, a), animate);
        this.say(a, `${e.tool}: ${(e.summary || "").replace(/^[a-z_ ]+of [^:]+: /, "").replace(/^[a-z_]+: /, "")}`, "", animate);
        if (fx) hits.slice(0, 24).forEach((u, i) => setTimeout(() => {
          this.line(this.desks[src] || a.at, this.desks[u] || this.roomAt[pathOf(u)], "#d6d6da", {ttl: 1800, cls: "thin"});
          this.pulse(u);
        }, 120 + i * 40));
        break;
      }
      case "decision":
        if (e.advisory && e.verdict === "acknowledged") {
          const theirs = p[p.length - 3];
          this.say(a, `confirmed: "${(e.reason || "").slice(0, 70)}${(e.reason || "").length > 70 ? "…" : ""}"`, "go", animate);
          if (fx) this.line(a.at, this.desks[theirs] || this.roomAt[pathOf(theirs)], VCOL.acknowledged, {ttl: 2400});
        } else if (e.advisory) {
          const theirs = p[p.length - 3], owner = p[p.length - 1];
          if (this.near(p[0], a)) this.move(a, this.near(p[0], a), animate);
          this.say(a, `possible overlap: reaches ${short(theirs)} (${who(owner)})`, "wait", animate);
          if (fx) { this.line(a.at, this.desks[theirs] || this.roomAt[pathOf(theirs)], VCOL.defer, {dashed: true, ttl: 3600}); this.pulse(theirs); }
        } else if (e.verdict === "proceed") {
          const targets = ((e.path || "").split("-HOLDS-> ")[1] || "").split(", ").filter(u => /^(fn|region|file|class):/.test(u));
          if (targets[0] && this.near(targets[0], a)) this.move(a, this.near(targets[0], a), animate);
          targets.forEach(u => { this.own(u, e.agent); if (fx) this.pulse(u); });
          this.say(a, `editing ${targets.slice(0, 2).map(short).join(", ")}`, "go", animate);
        } else if (e.verdict === "defer") {
          const unit = p[2], owner = p[4];
          const spot = this.near(unit, a);
          if (spot) this.move(a, {x: spot.x - 60, y: spot.y + 50}, animate);
          this.say(a, `waiting: ${who(owner)} is on ${short(unit)}`, "wait", animate);
          if (fx && this.agents[owner]) this.line(a.at, this.agents[owner].at, VCOL.defer, {dashed: true, ttl: 3000});
        } else if (e.verdict === "handoff") {
          const prev = p[0], unit = p[4];
          if (this.near(unit, a)) this.move(a, this.near(unit, a), animate);
          this.own(unit, e.agent);
          this.say(a, `took over ${short(unit)} from ${who(prev)}: merge first`, "hand", animate);
          if (fx) this.baton(this.agents[prev]?.at || this.desks[unit], this.desks[unit] || a.at, VCOL.handoff);
        } else if (e.verdict === "escalate") {
          this.say(a, "stopped: asked a human", "block", animate);
        }
        break;
      case "fact":
        if (fx) this.card(a, e.status === "observed" ? (e.text || "").replace(/^`[^`]*`:\s*/, "check: ") : e.status === "source-checked"
          ? "shared a checked fact" : "claim not verified", e.status === "unverified" || /fail|error/i.test(e.text || "") ? "block" : "go");
        break;
      case "stale-write":
        this.say(a, "wrote code it no longer held", "block", animate);
        break;
      case "release":
        (e.units || []).forEach(u => this.own(u, null));
        break;
      case "done": case "subagent-stop": case "session-end":
        a.done = true;
        a.el.classList.add("lv-done");
        this.say(a, "done", "go", animate);
        this.move(a, a.home, animate);
        for (const [uid, who_] of Object.entries(this.owner)) {  // its desks keep its colour, dashed: work finished
          if (who_ !== e.agent) continue;
          const el = this.root.querySelector(`.lv-desk[data-uid="${CSS.escape(uid)}"]`);
          if (el) el.dataset.state = "worked";
        }
        break;
    }
    const active = Object.values(this.agents).filter(x => !x.done).length;
    this.caption(`${active} agent${active === 1 ? "" : "s"} working · ${Object.keys(this.agents).length} in this run`);
  }

  // rebuild the scene as it was after `events` (scrubbing, replay, first load): state only, no animation
  replay(graph, events) {
    this.build(graph);
    for (const e of events) this.apply(e, false);
    requestAnimationFrame(() => Object.values(this.agents).forEach(a => a.el.classList.remove("lv-instant")));
  }
}
