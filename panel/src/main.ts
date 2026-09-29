/**
 * darkscope panel — every machine you run, on one graph.
 *
 * A pure receiver. It asks the server what machines exist and what they have
 * been saying, and draws it. It holds no addresses, talks to no node, and
 * knows nothing about how the data was collected — which is the point: the
 * collector runs next to your node and this runs wherever you like.
 */
import { PulsePlayer, httpTransport, PULSE_POLL_MS, LIVE_POLL_MS } from "./pulse-player";
import { mountGraph, type MachineSpec, type Mix } from "./graph";
import { machineColor } from "./palette";

type LiveNode = {
  node: string;
  height?: number;
  tip?: number;
  peers?: number;
  uptime?: number;
  memory?: { current?: number; high?: number; max?: number };
  receivedAt?: number;
};
type Live = { now: number; nodes: LiveNode[] };

const $ = (id: string) => document.getElementById(id)!;

/**
 * Everything drawn here arrives from a collector, and a collector is a machine
 * somebody else runs. The server types the numeric fields, but the page should
 * not depend on that: a peer count that is an <img onerror=...> ran in a browser
 * during the audit on 29-09-2026, so nothing reaches innerHTML unescaped.
 */
const esc = (v: unknown): string =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );

type Machine = MachineSpec & { desc?: string };

async function config(): Promise<Machine[]> {
  const r = await fetch("/api/config", { cache: "no-store" });
  const c = (await r.json()) as { machines: Machine[] };
  return c.machines ?? [];
}

const G = (b?: number) => (b ? (b / 1073741824).toFixed(2) + " G" : "—");
const up = (s?: number) => {
  if (!s) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};

function cards(live: Live, machines: Machine[]) {
  const by = new Map(live.nodes.map((n) => [n.node, n]));
  $("cards").innerHTML = machines
    .map((m) => {
      const n = by.get(m.id);
      const age = n?.receivedAt ? Math.round((live.now - n.receivedAt) / 1000) : null;
      const colour = machineColor(m.id, machines.map((x) => x.id));
      return `<div class="card">
        <div class="hd"><span class="dot" style="background:${colour}"></span>
          <b>${esc(m.label)}</b>${m.desc ? `<span class="desc">${esc(m.desc)}</span>` : ""}
          <span class="age">${age === null ? "no data" : esc(age) + "s old"}</span></div>
        <dl>
          <div><dt>height</dt><dd>${esc(n?.height?.toLocaleString() ?? "—")}</dd>
               <dfn>${n && n.height === n.tip ? "at tip" : n?.tip ? "tip " + esc(n.tip) : ""}</dfn></div>
          <div><dt>memory held</dt><dd>${esc(G(n?.memory?.current))}</dd>
               <dfn>${n?.memory?.high ? "of " + esc(G(n.memory.high)) : "no limit"}</dfn></div>
          <div><dt>peers</dt><dd>${esc(n?.peers ?? "—")}</dd><dfn></dfn></div>
          <div><dt>uptime</dt><dd>${esc(up(n?.uptime))}</dd><dfn></dfn></div>
        </dl></div>`;
    })
    .join("");
}

function mix(m: Mix) {
  const keys = ["discovery", "keepalive", "chain", "tx"] as const;
  const tot = keys.reduce((a, k) => a + (m[k] ?? 0), 0) || 1;
  $("mix").innerHTML =
    keys
      .map((k) => `<span class="sw ${k}"></span>${k} <b>${Math.round((100 * (m[k] ?? 0)) / tot)}%</b>`)
      .join("") + `<span class="tot">${tot.toLocaleString()} messages, last 5 min</span>`;
}

(async () => {
  const machines = await config();
  if (!machines.length) {
    $("cards").innerHTML = `<div class="card"><b>no machines configured</b></div>`;
    return;
  }
  const player = new PulsePlayer();
  const transport = httpTransport<Live>();

  mountGraph($("net"), { player, machines, onMix: mix, onFps: () => {} });

  let since: string | null = null;
  const pulse = async () => {
    try {
      const sent = Date.now();
      const res = await transport.pulse(since);
      player.ingest(res, sent, Date.now());
      since = res.cursor ?? since;
    } catch {
      /* a dropped poll is a second of missing animation, never a crash */
    }
  };
  const live = async () => {
    try {
      cards(await transport.live(), machines);
    } catch {}
  };
  await Promise.all([pulse(), live()]);
  setInterval(pulse, PULSE_POLL_MS);
  setInterval(live, LIVE_POLL_MS);
})();
