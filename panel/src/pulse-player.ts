/**
 * The clock behind `/darknode/live`. Nothing here draws.
 *
 * The page does not paint what a poll returns. It appends every batch to a
 * buffer and plays the buffer on its own clock, DELAY behind the server's.
 * That gives three things at once:
 *
 *   - motion that never stutters: a late or lost poll just means the buffer
 *     runs a little shorter, and there is still work queued to draw;
 *   - one order across two machines whose clocks disagree: every event is put
 *     on the server's clock before it is queued (see `offset` below);
 *   - a fallback for free: the transport is swappable, and a recording is the
 *     poll responses themselves, so a replay runs through this same code.
 */

import type { PulseBatch, PulseDir, PulseResponse } from "./node-pulse";

export const PLAY_DELAY_MS = 2000;
export const PULSE_POLL_MS = 1000;
export const LIVE_POLL_MS = 5000;

/** An event placed on the server's clock, ready to play. */
export type PlayEvent = {
  /** Server-clock ms at which it happened. */
  at: number;
  node: string;
  peer: string;
  dir: PulseDir;
  cmd: string;
};

/** Offset candidates kept per machine; about a minute of batches. */
const OFFSET_WINDOW = 60;
/** Server/client skew samples. */
const SKEW_WINDOW = 15;

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export class PulsePlayer {
  readonly delayMs: number;
  private queue: PlayEvent[] = [];
  /** Per machine: server ms − machine ms, one candidate per batch. */
  private offsets = new Map<string, number[]>();
  /** Server ms − client ms. */
  private skews: number[] = [];
  private skew = 0;

  constructor(delayMs = PLAY_DELAY_MS) {
    this.delayMs = delayMs;
  }

  /**
   * `sentAt`/`recvAt` are the client's clock around the request, which is how
   * the server's `now` is turned into a skew.
   */
  ingest(res: PulseResponse, sentAt: number, recvAt: number): void {
    this.skews.push(res.now - (sentAt + recvAt) / 2);
    if (this.skews.length > SKEW_WINDOW) this.skews.shift();
    this.skew = median(this.skews);

    for (const b of res.batches) this.ingestBatch(b);
  }

  /**
   * A machine's clock is mapped onto the server's by the smallest gap seen
   * between the newest event in a batch and the batch's arrival: that is the
   * batch that travelled fastest, so it is the closest either clock gets to
   * the other. The minimum only moves when a faster batch shows up, which
   * keeps one machine's events in the order the machine stamped them.
   */
  private ingestBatch(b: PulseBatch): void {
    if (!b.events.length) return;
    const newest = b.events[b.events.length - 1].t / 1000;
    const c = this.offsets.get(b.node) ?? [];
    c.push(b.at - newest);
    if (c.length > OFFSET_WINDOW) c.shift();
    this.offsets.set(b.node, c);
    const offset = Math.min(...c);

    for (const e of b.events) {
      this.insert({ at: e.t / 1000 + offset, node: b.node, peer: e.peer, dir: e.dir, cmd: e.cmd });
    }
  }

  private insert(ev: PlayEvent): void {
    const q = this.queue;
    // Batches arrive nearly in order, so walk back from the end.
    let i = q.length;
    while (i > 0 && q[i - 1].at > ev.at) i--;
    q.splice(i, 0, ev);
  }

  /** The server's clock as the client sees it now, minus the delay. */
  playhead(clientNow: number): number {
    return clientNow + this.skew - this.delayMs;
  }

  /**
   * Everything due by now, oldest first. An event that arrived after its slot
   * is still returned; the caller decides what "too late to draw" means.
   */
  drain(clientNow: number): PlayEvent[] {
    const head = this.playhead(clientNow);
    let n = 0;
    while (n < this.queue.length && this.queue[n].at <= head) n++;
    return n ? this.queue.splice(0, n) : [];
  }

  /** Buffered ahead of the playhead, ms — how much slack there is. */
  ahead(clientNow: number): number {
    if (!this.queue.length) return 0;
    return this.queue[this.queue.length - 1].at - this.playhead(clientNow);
  }
}

// --- transports --------------------------------------------------------------

/**
 * Where the page gets its data. Live reads the two routes; a replay hands back
 * recorded responses on the recording's own schedule, shifted to now.
 */
export interface Transport<L> {
  pulse(since: string | null): Promise<PulseResponse>;
  live(): Promise<L>;
}

export function httpTransport<L>(): Transport<L> {
  return {
    async pulse(since) {
      const q = since ? `?since=${encodeURIComponent(since)}` : "";
      const r = await fetch(`/api/node-pulse${q}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`pulse ${r.status}`);
      return (await r.json()) as PulseResponse;
    },
    async live() {
      const r = await fetch("/api/node-live", { cache: "no-store" });
      if (!r.ok) throw new Error(`live ${r.status}`);
      return (await r.json()) as L;
    },
  };
}

/** A recording: the responses the page received, and when. */
export type Recording<L, X = unknown> = {
  v: 1;
  /** Client ms when recording started. */
  startedAt: number;
  /** Anything the page was rendered with that did not come from a poll. */
  extra?: X;
  frames: (
    | { dt: number; kind: "pulse"; res: PulseResponse }
    | { dt: number; kind: "live"; res: L }
  )[];
};

export class Recorder<L, X = unknown> {
  private rec: Recording<L, X>;
  constructor(extra?: X) {
    this.rec = { v: 1, startedAt: Date.now(), extra, frames: [] };
  }
  pulse(res: PulseResponse) {
    if (res.batches.length) {
      this.rec.frames.push({ dt: Date.now() - this.rec.startedAt, kind: "pulse", res });
    }
  }
  live(res: L) {
    this.rec.frames.push({ dt: Date.now() - this.rec.startedAt, kind: "live", res });
  }
  get length() {
    return this.rec.frames.length;
  }
  finish(): Recording<L, X> {
    return this.rec;
  }
}

/**
 * Every server-clock and machine-clock stamp in a response, moved by `shift`
 * ms. What a replay needs so the recorded minutes look like the current ones.
 */
export type Shifter<L> = (res: L, shift: number) => L;

function shiftPulse(res: PulseResponse, shift: number): PulseResponse {
  return {
    now: res.now + shift,
    cursor: res.cursor,
    batches: res.batches.map((b) => ({
      node: b.node,
      at: b.at + shift,
      events: b.events.map((e) => ({ ...e, t: e.t + shift * 1000 })),
    })),
  };
}

/**
 * Plays a recording back as if it were the server, and loops it. The pulse
 * cursor is ignored: each call returns what the recording received since the
 * previous call, in the order it was received.
 */
export function replayTransport<L>(rec: Recording<L>, shiftLive: Shifter<L>): Transport<L> {
  const frames = rec.frames;
  const span = frames.length ? frames[frames.length - 1].dt + PULSE_POLL_MS : 1;
  const firstNow = frames.find((f) => f.kind === "pulse")?.res.now;
  const start = Date.now();
  // Server stamps in the recording move so its first poll reads as now.
  const base = firstNow !== undefined ? start - firstNow : start - rec.startedAt;
  let pulseIdx = 0;
  let loop = 0;
  let lastLive: L | null = null;

  const elapsed = () => Date.now() - start - loop * span;

  return {
    async pulse() {
      if (elapsed() >= span) {
        loop++;
        pulseIdx = 0;
      }
      const shift = base + loop * span;
      const t = elapsed();
      const batches: PulseBatch[] = [];
      let now = Date.now() - shift;
      while (pulseIdx < frames.length && frames[pulseIdx].dt <= t) {
        const f = frames[pulseIdx++];
        if (f.kind === "pulse") {
          batches.push(...f.res.batches);
          now = f.res.now;
        }
      }
      return shiftPulse({ now, cursor: "0-0", batches }, shift);
    },
    async live() {
      const t = elapsed();
      const shift = base + loop * span;
      for (const f of frames) {
        if (f.dt > t) break;
        if (f.kind === "live") lastLive = f.res;
      }
      lastLive ??= (frames.find((f) => f.kind === "live")?.res as L | undefined) ?? null;
      if (!lastLive) throw new Error("recording has no live frames");
      return shiftLive(lastLive, shift);
    },
  };
}
