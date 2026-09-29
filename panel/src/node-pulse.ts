/**
 * Contract for `/api/node-pulse`: the per-message P2P events each machine
 * reads from darkfid's dnet stream, batched about once a second.
 *
 * Addresses never leave the machines. A peer is a label the machine assigns:
 *
 *   - a public node everyone knows (a seed, one of the project's own nodes)
 *     goes by the same label on every machine, e.g. `seed-0`, so the page can
 *     tell that two machines hold the same peer;
 *   - one of our own machines goes by its node id (`darknode`, `darkfi-obs`),
 *     which is what draws the direct channel between them;
 *   - anyone else is `p<n>`, a first-seen index local to the machine that saw
 *     it — `p3` on one machine and `p3` on the other are different peers.
 *
 * The charset rules out anything that could be an address: no dots, no colons.
 */

/** A node id is what a machine calls itself: lowercase, short, no surprises.
 *  Inlined here so the panel has no dependency on the site it came from. */
const NODE_ID = /^[a-z0-9][a-z0-9-]{0,30}$/;
const isNodeId = (v: unknown): v is string => typeof v === "string" && NODE_ID.test(v);

export const PULSE_STREAM_KEY = "node:pulse";

/** How far back the buffer reaches. A page that opens late starts from here. */
export const PULSE_RETENTION_MS = 5 * 60 * 1000;

/** Hard body cap on POST /api/node-pulse. ~2 events/s makes a batch tiny. */
export const MAX_PULSE_BYTES = 64 * 1024;
export const MAX_PULSE_EVENTS = 1000;

/** Batches per GET. Five minutes at one a second from two machines is 600. */
export const MAX_PULSE_BATCHES = 600;

/**
 * Minimum gap between two accepted batches from one machine. The emitter posts
 * about once a second; this only stops a leaked token from hammering the store.
 */
export const MIN_PULSE_INTERVAL_MS = 250;

export type PulseDir = "send" | "recv";

export type PulseEvent = {
  /** The machine's own clock, µs since epoch. Not comparable across machines. */
  t: number;
  peer: string;
  dir: PulseDir;
  cmd: string;
};

export type PulseBatch = {
  node: string;
  /** Server receipt, ms. The one clock both machines share. */
  at: number;
  events: PulseEvent[];
};

export type PulseResponse = {
  /** Server time when the response was built, ms. */
  now: number;
  /** Pass back as `since`. Opaque: a Redis stream id. */
  cursor: string;
  batches: PulseBatch[];
};

const CMD_RE = /^[a-z_]{1,24}$/;
const CURSOR_RE = /^\d{1,15}-\d{1,10}$/;

export function isCursor(v: unknown): v is string {
  return typeof v === "string" && CURSOR_RE.test(v);
}

export function parsePulseBody(
  body: unknown,
): { ok: true; events: PulseEvent[] } | { ok: false; error: string } {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return { ok: false, error: "body must be a JSON object" };
  }
  const raw = (body as Record<string, unknown>).events;
  if (!Array.isArray(raw)) return { ok: false, error: "events must be an array" };
  if (raw.length > MAX_PULSE_EVENTS) return { ok: false, error: "too many events" };

  const events: PulseEvent[] = [];
  for (const e of raw as Record<string, unknown>[]) {
    if (
      !e ||
      typeof e.t !== "number" ||
      !Number.isFinite(e.t) ||
      e.t <= 0 ||
      !isNodeId(e.peer) ||
      (e.dir !== "send" && e.dir !== "recv") ||
      typeof e.cmd !== "string" ||
      !CMD_RE.test(e.cmd)
    ) {
      return { ok: false, error: "each event needs {t: µs, peer: [a-z0-9-], dir: send|recv, cmd}" };
    }
    events.push({ t: e.t, peer: e.peer, dir: e.dir, cmd: e.cmd });
  }
  events.sort((a, b) => a.t - b.t);
  return { ok: true, events };
}

/** What a message is for, which is what the pulse is coloured by. */
export type PulseClass = "discovery" | "keepalive" | "chain" | "tx";

const CLASS: Record<string, PulseClass> = {
  ping: "keepalive",
  pong: "keepalive",
  getaddr: "discovery",
  addr: "discovery",
  version: "discovery",
  verack: "discovery",
  // A transaction is the one message on this wire whose ORIGIN is meant to be
  // secret, so it gets its own class. Folded into "chain" it was the same colour
  // as the proposals that follow it half a second later, and a viewer could not
  // tell which pulse was the one that mattered.
  tx: "tx",
  tiprequest: "chain",
  tipresponse: "chain",
  proposal: "chain",
};

export function pulseClass(cmd: string): PulseClass {
  return CLASS[cmd] ?? "chain";
}
