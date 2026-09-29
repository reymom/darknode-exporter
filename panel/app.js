/*
 * darkscope panel — your node and the peers it is talking to, drawn from its own
 * debug feed.
 *
 * No dependencies on purpose. A canvas, a radial layout and a polling loop. A
 * force-directed WebGL graph looks better and needs a toolchain heavier than the
 * node it is watching, which would be an odd thing to ship with a project about
 * running one on a Raspberry Pi.
 *
 * The one thing worth understanding before you trust what you see: the EVENTS
 * and their TIMES are real, taken from darkfid to the millisecond. How long a
 * dot takes to cross the screen is NOT — it comes from how long that line is.
 * And the whole feed plays PLAY_DELAY behind, because events arrive in batches
 * and playing them the instant they land gives you clumps instead of motion.
 */
"use strict";

const POLL_MS = 1000;
const PLAY_DELAY = 2000;
const MIX_WINDOW = 5 * 60 * 1000;

const CLASS_OF = (cmd) => {
  if (cmd === "tx") return "tx";
  if (cmd === "proposal" || cmd.startsWith("tip") || cmd.startsWith("sync") || cmd.startsWith("fork")) return "chain";
  if (cmd === "getaddr" || cmd === "addr" || cmd === "version" || cmd === "verack") return "discovery";
  return "keepalive";
};
const COLOUR = {
  tx: "#e06c00",
  chain: "#d9489c",
  discovery: "#4dd2ff",
  keepalive: "#8b939c",
};
const LABELLED = new Set(["tx", "proposal", "addr"]);

const canvas = document.getElementById("net");
const ctx = canvas.getContext("2d");

const peers = new Map(); // id -> {angle, count, last}
let playing = []; // pulses in flight
let buffer = []; // events waiting for their play time
let cursor = 0;
let clockSkew = null; // server time - our time
let live = null;
let recent = []; // [t, class] for the mix

// --- polling -----------------------------------------------------------------

async function pollPulse() {
  try {
    const r = await fetch(`/api/pulse?since=${cursor}`, { cache: "no-store" });
    const d = await r.json();
    if (clockSkew === null) clockSkew = d.now - Date.now();
    cursor = d.cursor;
    for (const e of d.events) {
      if (!peers.has(e.peer)) peers.set(e.peer, { angle: 0, count: 0, last: 0 });
      const p = peers.get(e.peer);
      p.count += 1;
      p.last = e.t;
      buffer.push(e);
      recent.push([e.t, CLASS_OF(e.cmd)]);
    }
    if (d.events.length) layout();
  } catch (_) {
    /* a poll that fails is a second of missing animation, never a crash */
  }
}

async function pollLive() {
  try {
    const r = await fetch("/api/live", { cache: "no-store" });
    live = await r.json();
    drawCard();
  } catch (_) {}
}

// --- layout ------------------------------------------------------------------

function layout() {
  // Peers sit on a circle, ordered by when we first heard of them, so the shape
  // is stable between frames and a node does not jump when traffic changes.
  const ids = [...peers.keys()];
  ids.forEach((id, i) => {
    peers.get(id).angle = (i / ids.length) * Math.PI * 2 - Math.PI / 2;
  });
}

function positions() {
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2;
  const r = Math.min(w, h) * 0.34;
  const out = new Map();
  for (const [id, p] of peers) {
    out.set(id, { x: cx + Math.cos(p.angle) * r, y: cy + Math.sin(p.angle) * r });
  }
  return { cx, cy, out };
}

// --- drawing -----------------------------------------------------------------

function resize() {
  const d = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * d;
  canvas.height = canvas.clientHeight * d;
}

function frame() {
  const now = Date.now() + (clockSkew || 0) - PLAY_DELAY;

  // events whose moment has come become pulses
  let i = 0;
  while (i < buffer.length) {
    if (buffer[i].t <= now) {
      const e = buffer.splice(i, 1)[0];
      playing.push({ ...e, cls: CLASS_OF(e.cmd), born: performance.now() });
    } else i++;
  }
  if (buffer.length > 6000) buffer = buffer.slice(-3000);

  const cut = now - MIX_WINDOW;
  while (recent.length && recent[0][0] < cut) recent.shift();

  const { cx, cy, out } = positions();
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // edges
  ctx.lineWidth = 1;
  for (const [id, pos] of out) {
    const p = peers.get(id);
    const hot = now - p.last < 4000;
    ctx.strokeStyle = hot ? "rgba(232,255,227,0.28)" : "rgba(232,255,227,0.10)";
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(pos.x, pos.y);
    ctx.stroke();
  }

  // pulses
  const t = performance.now();
  playing = playing.filter((pl) => {
    const pos = out.get(pl.peer);
    if (!pos) return false;
    const len = Math.hypot(pos.x - cx, pos.y - cy);
    const dur = Math.min(1400, Math.max(600, len / 0.32 / (window.devicePixelRatio || 1)));
    const k = (t - pl.born) / dur;
    if (k >= 1) return false;
    const a = pl.dir === "send" ? { x: cx, y: cy } : pos;
    const b = pl.dir === "send" ? pos : { x: cx, y: cy };
    const x = a.x + (b.x - a.x) * k;
    const y = a.y + (b.y - a.y) * k;
    const c = COLOUR[pl.cls];
    const rr = (pl.cls === "tx" ? 6 : pl.cls === "chain" ? 5 : 3.5) * (window.devicePixelRatio || 1);
    ctx.globalAlpha = 1 - k * 0.25;
    ctx.fillStyle = c;
    ctx.shadowColor = c;
    ctx.shadowBlur = 16;
    ctx.beginPath();
    ctx.arc(x, y, rr, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.globalAlpha = 1;
    if (LABELLED.has(pl.cmd)) {
      ctx.fillStyle = "rgba(11,13,12,0.85)";
      const wlab = ctx.measureText(pl.cmd).width + 12;
      ctx.fillRect(x + 10, y - 18, wlab, 22);
      ctx.fillStyle = c;
      ctx.font = `${12 * (window.devicePixelRatio || 1)}px ui-monospace, monospace`;
      ctx.fillText(pl.cmd, x + 16, y - 2);
    }
    return true;
  });

  // peers
  for (const [id, pos] of out) {
    ctx.strokeStyle = "rgba(232,255,227,0.45)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, 6 * (window.devicePixelRatio || 1), 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = "rgba(232,255,227,0.40)";
    ctx.font = `${11 * (window.devicePixelRatio || 1)}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.fillText(id, pos.x, pos.y + 22 * (window.devicePixelRatio || 1));
  }

  // my node
  ctx.strokeStyle = "#39ff14";
  ctx.lineWidth = 2.5;
  ctx.shadowColor = "#39ff14";
  ctx.shadowBlur = 18;
  ctx.beginPath();
  ctx.arc(cx, cy, 13 * (window.devicePixelRatio || 1), 0, Math.PI * 2);
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.fillStyle = "#39ff14";
  ctx.font = `${13 * (window.devicePixelRatio || 1)}px ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.fillText(live?.node || "my node", cx, cy + 34 * (window.devicePixelRatio || 1));

  drawMix();
  requestAnimationFrame(frame);
}

function drawMix() {
  const el = document.getElementById("mix");
  if (!recent.length) {
    el.textContent = "waiting for traffic…";
    return;
  }
  const n = { discovery: 0, keepalive: 0, chain: 0, tx: 0 };
  for (const [, c] of recent) n[c]++;
  const tot = recent.length;
  el.innerHTML = ["discovery", "keepalive", "chain", "tx"]
    .map(
      (k) =>
        `<span class="sw" style="background:${COLOUR[k]}"></span>${k} <b>${Math.round(
          (100 * n[k]) / tot
        )}%</b>`
    )
    .join("") + `<span class="tot">${tot} messages, last 5 min</span>`;
}

function fmtBytes(b) {
  return b ? (b / 1073741824).toFixed(2) + " G" : "—";
}
function fmtUp(s) {
  if (!s) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function drawCard() {
  const el = document.getElementById("card");
  if (!live || !live.ok) {
    el.innerHTML = `<div class="k">no snapshot yet</div><div class="v">is darknode-export running?</div>`;
    return;
  }
  const rows = [
    ["height", live.height?.toLocaleString() ?? "—", live.height === live.tip ? "at tip" : `tip ${live.tip}`],
    ["memory held", fmtBytes(live.held), live.high ? `of ${fmtBytes(live.high)}` : "no limit"],
    ["peers", live.peers ?? "—", ""],
    ["uptime", fmtUp(live.uptime), live.tempC ? `${live.tempC}°C` : ""],
  ];
  el.innerHTML = rows
    .map(([k, v, s]) => `<div><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`)
    .join("");
}

window.addEventListener("resize", resize);
resize();
pollLive();
pollPulse();
setInterval(pollPulse, POLL_MS);
setInterval(pollLive, 15000);
requestAnimationFrame(frame);
