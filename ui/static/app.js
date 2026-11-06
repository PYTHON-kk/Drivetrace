// DriveTrace UI - vanilla JS, no external libraries (works fully offline).
// All server text is inserted with textContent, never innerHTML: text inside vehicle logs cannot run as code.
"use strict";

const EXAMPLES = [
  "Why has my driving range reduced over the last two weeks?",
  "When did fuel efficiency show the largest drop?",
  "Show all ADAS disengagement events from yesterday.",
  "What happened before the battery overheating warning?",
  "Compare energy consumption between city and highway driving.",
  "How many battery overheating warnings this month?",
  "Show my energy consumption trend over the last 14 days",
  "Brake hard now",
];
const ROLE_LABELS = { driver: "Driver", service: "Service", fleet: "Fleet" };
const STAGE_COLORS = { route: "#8c959f", plan: "#bf8700", validate: "#6e7781", execute: "#1f6feb", render: "#1a7f37" };
const LEVEL_TEXT = { ATTRIBUTED: "cause (model-checked)", CONSISTENT_WITH: "consistent with", ASSOCIATED: "vs baseline",
                     OBSERVED: "observed", INSUFFICIENT: "insufficient" };
const NUM = /[-+]?\d[\d,]*(?:[.:]\d+)?%?/g;  // "11:15" is one token

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
let role = "service";

function setRole(r) {
  role = r;
  for (const b of $("roles").children) b.setAttribute("aria-checked", String(b.dataset.role === r));
  try { localStorage.setItem("dt.role", r); } catch (_) {}
}

async function init() {
  try { role = localStorage.getItem("dt.role") || role; } catch (_) {}
  const res = await fetch("/api/sources");
  const data = await res.json();
  for (const s of data.sources) {
    const o = el("option", null, `${s.key} — ${s.source_type} (${s.dataset})`);
    o.value = s.key;
    $("source").append(o);
  }
  for (const r of data.roles) {
    const b = el("button", null, ROLE_LABELS[r] || r);
    b.type = "button"; b.dataset.role = r; b.setAttribute("role", "radio");
    b.onclick = () => setRole(r);
    $("roles").append(b);
  }
  setRole(role);
  for (const q of EXAMPLES) {
    const b = el("button", null, q);
    b.type = "button";
    b.onclick = () => { $("q").value = q; ask(); };
    $("examples").append(b);
  }
  $("ask").onsubmit = (e) => { e.preventDefault(); ask(); };
}

function highlighted(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(NUM)) {
    frag.append(document.createTextNode(text.slice(last, m.index)));
    frag.append(el("span", "num", m[0]));
    last = m.index + m[0].length;
  }
  frag.append(document.createTextNode(text.slice(last)));
  return frag;
}

async function ask() {
  const question = $("q").value.trim();
  if (!question) return;
  $("go").disabled = true;
  const t0 = performance.now();
  try {
    const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, source: $("source").value, role }) });
    const a = await res.json();
    if (!res.ok) throw new Error(a.detail || res.statusText);
    show(a, performance.now() - t0);
  } catch (err) {
    $("answer").className = "answertext";
    $("answer").textContent = "Error: " + err.message;
  } finally {
    $("go").disabled = false;
  }
}

function show(a, roundTrip) {
  const ans = $("answer");
  ans.className = "answertext";
  ans.replaceChildren(highlighted(a.text));
  $("notes").textContent = "";
  $("tier").textContent = a.tier === "tier0" ? `rules · ${a.rule}` : a.tier === "tier1" ? "SLM planner" : "not understood";
  $("plan").textContent = a.plan ? JSON.stringify(a.plan, null, 1) : "—";

  // latency
  $("total").textContent = `${a.total_ms.toFixed(0)} ms`;
  const bar = $("latbar"); bar.replaceChildren();
  const list = $("stages"); list.replaceChildren();
  for (const [stage, ms] of Object.entries(a.timings_ms)) {
    const s = el("span"); s.style.width = `${Math.min(100, ms / 20)}%`; s.style.background = STAGE_COLORS[stage] || "#999";
    s.title = `${stage}: ${ms} ms`; bar.append(s);
    const li = el("li"); const name = el("span"); const sw = el("i"); sw.style.background = STAGE_COLORS[stage] || "#999";
    name.append(sw, document.createTextNode(stage)); li.append(name, el("span", null, `${ms.toFixed(1)} ms`)); list.append(li);
  }
  const rt = el("li"); rt.append(el("span", null, "browser round trip"), el("span", null, `${roundTrip.toFixed(0)} ms`)); list.append(rt);

  // evidence
  const claims = $("claims"); claims.replaceChildren();
  const d = a.details || {};
  for (const c of d.claims || []) {
    const row = el("div", "claim");
    row.append(el("span", null, c.label), el("span", `badge lvl-${c.level}`, LEVEL_TEXT[c.level] || c.level));
    claims.append(row);
  }
  if (d.gate) claims.append(el("p", "hint", `Evidence gate: ${d.gate.toLowerCase()} · baseline ${d.baseline.status} (${d.baseline.sessions.length} sessions) · record v${d.record_version}`));
  const ev = $("evidence"); ev.replaceChildren();
  for (const e of a.evidence || []) ev.append(el("li", null, e));
  if (!(a.evidence || []).length && !(d.claims || []).length) ev.append(el("li", null, "No evidence items for this answer."));

  drawCharts(a.charts || []);
}

function drawCharts(charts) {
  const card = $("chartcard"), box = $("charts");
  box.replaceChildren();
  card.hidden = charts.length === 0;
  const tz = (charts.find((c) => c.tz) || {}).tz;
  $("charthint").textContent = charts.some((c) => c.marker) ? `red line = event time · times in ${tz}` : "";
  for (const c of charts) box.append(chart(c));
}

function chart(c) {
  const W = 600, H = 170, L = 46, R = 10, T = 10, B = 24;
  const wrap = el("div", "chart");
  wrap.append(el("h3", null, `${c.title}${c.unit ? " (" + c.unit + ")" : ""}`));
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", c.title);
  const pts = c.points;
  const xs = c.x === "time" ? pts.map((p) => p[0]) : pts.map((_, i) => i);
  const ys = pts.map((p) => p[1]);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (c.kind === "bar") y0 = 0;
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.08; y0 -= c.kind === "bar" ? 0 : pad; y1 += pad;
  const x0 = Math.min(...xs), x1 = Math.max(...xs) || 1;
  const sx = (x) => L + ((x - x0) / ((x1 - x0) || 1)) * (W - L - R);
  const sy = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  const add = (tag, attrs, text) => { const n = document.createElementNS(ns, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); if (text != null) n.textContent = text; svg.append(n); return n; };
  for (const v of [y0 + pad, (y0 + y1) / 2, y1 - pad]) {
    add("line", { x1: L, x2: W - R, y1: sy(v), y2: sy(v), class: "grid" });
    add("text", { x: L - 6, y: sy(v) + 3, "text-anchor": "end", class: "axis" }, fmt(v));
  }
  const hhmm = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: c.tz || "UTC" });
  const xl = c.x === "time" ? (x) => hhmm.format(new Date(x)) : (i) => String(pts[i][0]).slice(5);
  add("text", { x: L, y: H - 6, class: "axis" }, xl(xs[0]));
  add("text", { x: W - R, y: H - 6, "text-anchor": "end", class: "axis" }, xl(xs[xs.length - 1]));
  if (c.kind === "bar") {
    const w = Math.max(2, (W - L - R) / pts.length - 4);
    pts.forEach((p, i) => add("rect", { x: sx(i) - w / 2, y: sy(p[1]), width: w, height: sy(0) - sy(p[1]), fill: "var(--bar)" }));
  } else {
    add("polyline", { points: xs.map((x, i) => `${sx(x).toFixed(1)},${sy(ys[i]).toFixed(1)}`).join(" "),
                      fill: "none", stroke: "var(--chart)", "stroke-width": 2, "stroke-linejoin": "round" });
  }
  if (c.marker) add("line", { x1: sx(c.marker), x2: sx(c.marker), y1: T, y2: H - B, stroke: "var(--marker)", "stroke-width": 1.5, "stroke-dasharray": "4 3" });
  wrap.append(svg);
  return wrap;
}

function fmt(v) {
  const a = Math.abs(v);
  return a >= 1000 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
}

init();
