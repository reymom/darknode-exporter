"use client";

/**
 * The network both machines are in, drawn as it talks.
 *
 * 3d-force-graph, for one reason: it renders through three.js, and three.js
 * is what makes post-processing possible. UnrealBloomPass is the glow,
 * AfterimagePass the wake a pulse leaves behind. The camera is nearly
 * orthographic and barely tilted, and the layout has two dimensions: this is
 * a flat network that glows, not an object to orbit.
 *
 * What moves, and why:
 *
 *   - one `emitParticle` per message the player hands over — never the
 *     built-in cycling particles, which would move whether anything happened
 *     or not. A particle rides the link from the side that sent it;
 *   - `alphaTarget` stays above zero, so the layout never quite settles;
 *   - edges widen with their share of their own machine's traffic and
 *     brighten with how recently they carried something; peers grow with
 *     their share. Never the share of the total — see the frame loop.
 *
 * Pulse labels are the one thing drawn outside three.js: see `tagged`.
 *
 * Bloom is a budget. Only what is brighter than 1 blooms: pulses (additive,
 * HDR, not tone-mapped) and edges that just carried one. Nodes, labels and
 * idle edges stay under the threshold and do not glow.
 *
 * Every graph object here uses shared geometries and materials whose dispose
 * is a no-op: the library frees an object's geometry and material when it
 * removes it, and a spent pulse must not take its class's material with it.
 */

import type { ForceGraph3DInstance } from "3d-force-graph";
import type * as THREE from "three";
import { forceCollide, forceX, forceY } from "d3-force";
import { pulseClass, type PulseClass } from "./node-pulse";
import type { PlayEvent, PulsePlayer } from "./pulse-player";
import {
  INK,
  machineColor,
  MACHINE_FALLBACK,
  PULSE_COLOR,
  SURFACE,
} from "./palette";

/** Traffic decays with this time constant, so share means "lately". */
const TRAFFIC_TAU_MS = 60_000;
/** A peer silent this long has left the picture. */
const PEER_GONE_MS = 180_000;
/** How long an edge stays lit after it carried something. */
const EDGE_GLOW_MS = 900;
/**
 * An event this far behind the playhead is from before the page opened (the
 * first read returns the whole five-minute buffer). It shapes the graph —
 * which peers exist, how much each carries — but emits nothing.
 */
const BACKLOG_MS = 5_000;
/** Pulse travel time, from edge length, bounded both ways. */
const PULSE_SPEED = 0.32; // px/ms
const PULSE_MIN_MS = 500;
const PULSE_MAX_MS = 1_400;
/** The mix under the graph covers this much played traffic. */
const MIX_WINDOW_MS = 5 * 60_000;

/** Only these are labelled; a label on every ping is noise. */
const LABELLED = new Set(["tx", "proposal", "tiprequest", "addr"]);
/**
 * Labels on screen at once, by frame width. A machine taking inbound peers
 * gets `addr` all day, and past a handful the labels are the noise they were
 * meant to keep out — on a phone, they cover the graph. Chain and tx labels always
 * show; it is the `addr` ones that wait for room.
 */
const maxLabels = (w: number) => (w < 640 ? 3 : 8);

const MACHINE_R = 20;
/** Near-orthographic: a narrow lens, far away. */
const FOV = 10;
const TILT = (12 * Math.PI) / 180;

/** HDR multipliers against the bloom threshold of 1. */
// A transaction is brighter and bigger than anything else on the wire on purpose:
// it is the one message whose origin is supposed to be secret, and the point of
// watching is to see it leave. The absolute values came down hard on 27-S: at
// gain 7 a node relaying to seventeen peers at once saturated the bloom into a
// single blob and the individual pulses stopped being readable.
const PULSE_GAIN: Record<PulseClass, number> = { tx: 3.4, chain: 2.6, discovery: 1.9, keepalive: 1.1 };
const PULSE_R: Record<PulseClass, number> = { tx: 5.5, chain: 4.5, discovery: 3.2, keepalive: 2.4 };
// An edge that carried nothing this second still exists, and the graph reads as
// a shape, not only as traffic. At 0.1 the quiet machine's links were invisible
// next to the bloom of the busy one's.
const EDGE_IDLE = 0.28;
const EDGE_HOT = 1.3;

type GNode = {
  id: string;
  machine: boolean;
  label: string;
  /** For a peer only one machine can name (`p<n>`), that machine. */
  owner: string | null;
  traffic: number;
  last: number;
  r: number;
  x?: number;
  y?: number;
  z?: number;
  fx?: number;
  fy?: number;
  __threeObj?: THREE.Object3D;
};

type GLink = {
  source: GNode;
  target: GNode;
  /** The other direction of a visible edge: invisible, exerts no force, carries pulses. */
  reverse: boolean;
  /** The visible edge this belongs to. */
  edge: GEdge;
  /** Set right before `emitParticle`, read by the particle factory. */
  next?: { cls: PulseClass; label: string | null };
  __threeObj?: THREE.Object3D;
};

type GEdge = {
  key: string;
  forward: GLink;
  back: GLink;
  traffic: number;
  last: number;
  /** Cross-section for the link mesh, by traffic share. Applied in
   *  linkPositionUpdate, which is the only place the library hands us the mesh. */
  width: number;
  material: THREE.MeshBasicMaterial;
};

export type Mix = Record<PulseClass, number>;

export type MachineSpec = { id: string; label: string };

/** The machines an edge belongs to: one, or both for the direct channel. */
function edgeMachines(e: GEdge): string[] {
  const out: string[] = [];
  if (e.forward.source.machine) out.push(e.forward.source.id);
  if (e.forward.target.machine) out.push(e.forward.target.id);
  return out;
}

function isLocalPeer(peer: string): boolean {
  return /^p\d+$/.test(peer);
}

function keep<T extends { dispose: () => void }>(o: T): T {
  o.dispose = () => {};
  return o;
}

/**
 * Mount the graph into an element and return a teardown.
 *
 * This was a React component on the site it came from; the body was already one
 * imperative block, so the only change is what hands it the element. Nothing
 * about the rendering differs — it is the same graph, deliberately, because the
 * point of publishing it is that you get the one in the talk and not a sketch
 * of it.
 */
export function mountGraph(
  wrap: HTMLElement,
  {
    player,
    machines,
    onMix,
    onFps,
  }: {
    player: PulsePlayer;
    machines: MachineSpec[];
    onMix?: (mix: Mix) => void;
    onFps?: (fps: number) => void;
  },
): () => void {
  const cb = { current: { onMix, onFps } };
  {
    let disposed = false;
    let cleanup = () => {};

    (async () => {
      // three.js touches window at import; load it in the browser only.
      const [{ default: ForceGraph3D }, T, { UnrealBloomPass }, { AfterimagePass }, { OutputPass }] =
        await Promise.all([
          import("3d-force-graph"),
          import("three"),
          import("three/examples/jsm/postprocessing/UnrealBloomPass.js"),
          import("three/examples/jsm/postprocessing/AfterimagePass.js"),
          import("three/examples/jsm/postprocessing/OutputPass.js"),
        ]);
      if (disposed) return;

      // --- shared, undisposable resources ----------------------------------

      const unitSphere = keep(new T.SphereGeometry(1, 16, 12));
      const ring = keep(new T.RingGeometry(0.78, 1, 48));
      const disc = keep(new T.CircleGeometry(1, 48));
      const edgeGeometry = keep(new T.CylinderGeometry(0.5, 0.5, 1, 6, 1, false));
      edgeGeometry.translate(0, 0.5, 0);
      edgeGeometry.rotateX(Math.PI / 2);

      const flat = (color: string, gain = 1) =>
        keep(new T.MeshBasicMaterial({ color: new T.Color(color).multiplyScalar(gain) }));
      const blackout = flat(SURFACE);
      const peerRing = flat(INK, 0.55);

      const pulseMaterial = {} as Record<PulseClass, THREE.MeshBasicMaterial>;
      for (const c of Object.keys(PULSE_COLOR) as PulseClass[]) {
        pulseMaterial[c] = keep(
          new T.MeshBasicMaterial({
            color: new T.Color(PULSE_COLOR[c]).multiplyScalar(PULSE_GAIN[c]),
            blending: T.AdditiveBlending,
            transparent: true,
            depthWrite: false,
            toneMapped: false,
          }),
        );
      }

      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const labelCache = new Map<string, THREE.SpriteMaterial & { aspect: number }>();
      /** Text as a sprite, kept below the bloom threshold. */
      function label(text: string, px: number, weight = 400): THREE.Sprite {
        const key = `${text}|${px}|${weight}`;
        let mat = labelCache.get(key);
        if (!mat) {
          const font = `${weight} ${px * dpr * 2}px ui-monospace, "JetBrains Mono", monospace`;
          const c = document.createElement("canvas");
          const g = c.getContext("2d")!;
          g.font = font;
          c.width = Math.ceil(g.measureText(text).width);
          c.height = Math.ceil(px * dpr * 2 * 1.4);
          g.font = font;
          g.fillStyle = INK;
          g.textBaseline = "middle";
          g.fillText(text, 0, c.height / 2);
          const tex = keep(new T.CanvasTexture(c));
          tex.colorSpace = T.SRGBColorSpace;
          mat = Object.assign(
            keep(
              new T.SpriteMaterial({
                map: tex,
                transparent: true,
                depthWrite: false,
                depthTest: false,
                color: new T.Color(0.85, 0.85, 0.85),
              }),
            ),
            { aspect: c.width / c.height },
          );
          labelCache.set(key, mat);
        }
        const s = new T.Sprite(mat);
        const h = px * 1.4;
        s.scale.set(h * mat.aspect, h, 1);
        s.renderOrder = 30;
        return s;
      }

      /** Smoothed frame time, ms. */
      let frameMs = 16.7;

      // --- pulse labels -------------------------------------------------------

      const tagLayer = document.createElement("div");
      tagLayer.style.cssText = "position:absolute;inset:0;pointer-events:none;overflow:hidden";
      const tagged: { photon: THREE.Object3D; el: HTMLElement }[] = [];
      function tagElement(text: string): HTMLElement {
        const el = document.createElement("span");
        el.textContent = text;
        el.style.cssText = `position:absolute;left:0;top:0;padding:1px 5px;font:500 13px var(--font-jetbrains-mono),ui-monospace,monospace;color:${INK};background:rgba(0,0,0,0.72);white-space:nowrap;will-change:transform`;
        tagLayer.appendChild(el);
        return el;
      }
      const projected = new T.Vector3();

      // --- graph -------------------------------------------------------------

      let w = wrap.clientWidth;
      let h = wrap.clientHeight;
      const machineIds = machines.map((m) => m.id);
      const nodes = new Map<string, GNode>();
      const edges = new Map<string, GEdge>();
      const mixLog: { at: number; cls: PulseClass }[] = [];
      const machineSeen = new Map<string, number>();

      /**
       * The machines sit along the long side of the frame, where their cards
       * are: left and right on a wide screen, top and bottom on a phone held
       * upright, where the cards stack. World origin in the middle.
       */
      const portrait = () => h > w;
      const long = () => (portrait() ? h : w);
      const short = () => (portrait() ? w : h);
      function anchor(id: string): [number, number] {
        const i = machineIds.indexOf(id);
        const n = machineIds.length;
        const t = n === 1 ? 0 : -0.3 + (0.6 * i) / (n - 1);
        // three.js y points up: the first machine is on top.
        return portrait() ? [0, -t * h] : [t * w, 0];
      }

      for (const m of machines) {
        const [x, y] = anchor(m.id);
        nodes.set(m.id, {
          id: m.id,
          machine: true,
          label: m.label,
          owner: null,
          traffic: 0,
          last: 0,
          r: MACHINE_R,
          x,
          y,
          fx: x,
          fy: y,
        });
      }

      const graph: ForceGraph3DInstance = new ForceGraph3D(wrap, {
        controlType: "orbit",
        rendererConfig: { antialias: true, alpha: false, powerPreference: "high-performance" },
      });

      // Bloom and afterimage run at canvas resolution; past 1.5× a projector
      // gains nothing and the frame rate pays for it.
      graph.renderer().setPixelRatio(Math.min(1.5, window.devicePixelRatio || 1));
      // The controls are disabled but still claim every touch on the canvas;
      // on a phone the page must scroll past the graph.
      graph.renderer().domElement.style.touchAction = "pan-y";

      graph
        .width(w)
        .height(h)
        .backgroundColor(SURFACE)
        .showNavInfo(false)
        .enableNavigationControls(false)
        .enableNodeDrag(false)
        .numDimensions(2)
        .cooldownTicks(Infinity)
        .cooldownTime(Infinity)
        .d3AlphaMin(0)
        .d3VelocityDecay(0.35)
        .nodeId("id")
        .nodeLabel((n) => {
          const g = n as GNode;
          if (g.machine) return "";
          const holders = machineIds.filter((m) => edges.has([m, g.id].sort().join("|")));
          return `${g.label || "peer"} · ${holders.length > 1 ? "held by both" : `held by ${holders[0] ?? g.owner ?? "—"}`}`;
        })
        .nodeThreeObject((n) => {
          const g = n as GNode;
          const o = new T.Group();
          if (g.machine) {
            const color = machineColor(g.id, machines.map((m) => m.id));
            const back = new T.Mesh(disc, blackout);
            back.scale.setScalar(MACHINE_R);
            const rim = new T.Mesh(ring, flat(color));
            rim.scale.setScalar(MACHINE_R);
            const core = new T.Mesh(disc, flat(color));
            core.scale.setScalar(6);
            const tag = label(g.label, 15, 600);
            tag.position.set(0, -MACHINE_R - 16, 1);
            o.add(back, rim, core, tag);
          } else {
            const body = new T.Group();
            body.name = "body";
            body.add(new T.Mesh(disc, blackout), new T.Mesh(ring, peerRing));
            body.scale.setScalar(g.r);
            o.add(body);
            if (g.label) {
              const tag = label(g.label, 12);
              tag.name = "tag";
              tag.position.set(0, -g.r - 12, 1);
              o.add(tag);
            }
          }
          o.renderOrder = 20;
          return o;
        })
        .linkVisibility((l) => !(l as unknown as GLink).reverse)
        .linkWidth(1)
        .linkThreeObject((l) => {
          const link = l as unknown as GLink;
          const m = new T.Mesh(edgeGeometry, link.edge.material);
          m.renderOrder = 10;
          return m;
        })
        .linkPositionUpdate((obj, { start, end }, l) => {
          const a = new T.Vector3(start.x, start.y, start.z);
          const b = new T.Vector3(end.x, end.y, end.z);
          // The width lives here because `link.__threeObj` is never populated —
          // measured 27-S: it is undefined for EVERY edge, so the render loop's
          // scale assignment had never run and every link was one unit thick.
          // Short links survive that; the direct machine-to-machine link is 1152
          // units long and a one-unit sliver of it is invisible, which is why the
          // two machines looked unconnected while pulses ran between them.
          const gw = (l as unknown as GLink).edge?.width ?? 1;
          obj.scale.x = gw;
          obj.scale.y = gw;
          obj.position.copy(a);
          obj.scale.z = a.distanceTo(b) || 0.001;
          obj.lookAt(b);
          return true;
        })
        .linkDirectionalParticleSpeed((l) => {
          const link = l as unknown as GLink;
          const len = Math.hypot(
            (link.target.x ?? 0) - (link.source.x ?? 0),
            (link.target.y ?? 0) - (link.source.y ?? 0),
          );
          const dur = Math.min(PULSE_MAX_MS, Math.max(PULSE_MIN_MS, len / PULSE_SPEED));
          // The library advances a particle by this much per frame; scaling by
          // the measured frame time keeps a pulse's duration the same at 30 fps
          // as at 60.
          return frameMs / dur;
        })
        .linkDirectionalParticleThreeObject((l) => {
          const next = (l as unknown as GLink).next ?? { cls: "keepalive" as PulseClass, label: null };
          const m = new T.Mesh(unitSphere, pulseMaterial[next.cls]);
          const r = PULSE_R[next.cls];
          m.scale.setScalar(r);
          m.renderOrder = 25;
          // The label rides on the particle but is drawn as HTML over the
          // canvas: through the afterimage pass text smears into a wake of
          // itself, and a label is there to be read.
          if (next.label && (next.label !== "addr" || tagged.length < maxLabels(w))) {
            tagged.push({ photon: m, el: tagElement(next.label) });
          }
          return m;
        });

      graph.d3Force("center", null);
      graph.d3Force("charge")?.strength((n: GNode) => (n.machine ? -300 : -220));
      graph
        .d3Force("link")
        ?.distance((l: GLink) => {
          if (l.source.machine && l.target.machine) return 0.6 * long();
          const peer = l.source.machine ? l.target : l.source;
          if (peer.owner) return Math.min(110, 0.12 * short() + 40);
          // A shared peer belongs to both, so it sits between them: the
          // distance is the half-gap plus a bow that fits inside the frame.
          // Too long a link cannot be satisfied once the frame clips the bow,
          // and the peer then falls toward whichever machine wins the tug —
          // the busier one — and reads as that machine's own.
          const halfGap = 0.3 * long();
          const bow = Math.min(0.3 * short(), 0.1 * long());
          return Math.hypot(halfGap, bow);
        })
        .strength((l: GLink) => (l.reverse || (l.source.machine && l.target.machine) ? 0 : 0.35));
      // Plain d3-force forces: they only read x/y, which is all a 2D layout has.
      // (The casts are the library typing its nodes as NodeObject.)
      graph.d3Force("collide", forceCollide<GNode>().radius((d) => d.r + (d.label ? 26 : 10)) as never);
      // Owned peers are pulled toward their machine; shared ones toward the
      // middle, hard along the line between the machines, softly across it.
      const along = (d: GNode, isX: boolean) => (portrait() ? !isX : isX) && !d.owner;
      const towardX = forceX<GNode>((d) => (d.owner ? anchor(d.owner)[0] : 0)).strength((d) =>
        d.owner ? 0.04 : along(d, true) ? 0.08 : 0.012,
      );
      const towardY = forceY<GNode>((d) => (d.owner ? anchor(d.owner)[1] : 0)).strength((d) =>
        d.owner ? 0.04 : along(d, false) ? 0.08 : 0.012,
      );
      graph.d3Force("x", towardX as never);
      graph.d3Force("y", towardY as never);

      // --- post-processing: bloom, then the wake -----------------------------

      const composer = graph.postProcessingComposer();
      const bloom = new UnrealBloomPass(new T.Vector2(w, h), 0.85, 0.3, 1.0);
      const trails = new AfterimagePass(0.78);
      composer.addPass(bloom);
      composer.addPass(trails);
      composer.addPass(new OutputPass());

      // --- camera ------------------------------------------------------------

      function frame() {
        const cam = graph.camera() as THREE.PerspectiveCamera;
        cam.fov = FOV;
        // One world unit is one CSS pixel on the plane of the graph.
        const d = h / 2 / Math.tan((FOV * Math.PI) / 360);
        cam.position.set(0, -d * Math.sin(TILT), d * Math.cos(TILT));
        cam.near = d * 0.5;
        cam.far = d * 2;
        cam.up.set(0, 1, 0);
        cam.lookAt(0, 0, 0);
        cam.updateProjectionMatrix();
      }

      function push() {
        graph.graphData({
          nodes: [...nodes.values()],
          links: [...edges.values()].flatMap((e) => [e.forward, e.back]),
        } as never);
      }

      function resize() {
        w = wrap!.clientWidth;
        h = wrap!.clientHeight;
        if (!w || !h) return;
        graph.width(w).height(h);
        bloom.resolution.set(w, h);
        for (const id of machineIds) {
          const n = nodes.get(id)!;
          [n.fx, n.fy] = anchor(id);
        }
        // Targets, strengths and distances are read once per initialise;
        // setting them again makes the forces re-read the new frame.
        towardX.x(towardX.x()).strength(towardX.strength());
        towardY.y(towardY.y()).strength(towardY.strength());
        const link = graph.d3Force("link");
        link?.distance(link.distance());
        frame();
      }

      // --- events ------------------------------------------------------------

      function peerNode(machine: string, peer: string, now: number): GNode {
        const id = isLocalPeer(peer) ? `${machine}:${peer}` : peer;
        let n = nodes.get(id);
        if (!n) {
          const [ax, ay] = anchor(machine);
          n = {
            id,
            machine: false,
            label: isLocalPeer(peer) ? "" : peer,
            owner: isLocalPeer(peer) ? machine : null,
            traffic: 0,
            last: now,
            r: 4,
            // New peers enter near the machine that met them.
            x: ax + (Math.random() - 0.5) * 60,
            y: ay + (Math.random() - 0.5) * 60,
          };
          nodes.set(id, n);
        }
        return n;
      }

      function edgeBetween(a: GNode, b: GNode): GEdge {
        const [s, t] = a.id < b.id ? [a, b] : [b, a];
        const key = `${s.id}|${t.id}`;
        let e = edges.get(key);
        if (!e) {
          const material = new T.MeshBasicMaterial({
            color: new T.Color(INK).multiplyScalar(EDGE_IDLE),
            blending: T.AdditiveBlending,
            transparent: true,
            depthWrite: false,
            toneMapped: false,
          });
          e = { key, traffic: 0, last: 0, width: 1, material } as GEdge;
          e.forward = { source: s, target: t, reverse: false, edge: e };
          e.back = { source: t, target: s, reverse: true, edge: e };
          edges.set(key, e);
        }
        return e;
      }

      // Pulses on a link added this frame wait one frame, until the library
      // has digested it.
      let pending: { link: GLink; next: NonNullable<GLink["next"]> }[] = [];

      function play(ev: PlayEvent, now: number, head: number): boolean {
        const machine = nodes.get(ev.node);
        if (!machine?.machine) return false;
        const late = head - ev.at;
        const backlog = late > BACKLOG_MS;
        // Weight as if it had been decaying since it happened.
        const weight = backlog ? Math.exp(-late / TRAFFIC_TAU_MS) : 1;
        if (!backlog) machineSeen.set(ev.node, now);

        const direct = machineIds.includes(ev.peer) && ev.peer !== ev.node;
        // Both ends of the direct channel report the same message; the sender's
        // copy draws it, the receiver's only when the sender is not posting.
        if (direct && ev.dir === "recv" && now - (machineSeen.get(ev.peer) ?? 0) < 10_000) {
          return false;
        }

        const before = nodes.size + edges.size;
        const other = direct ? nodes.get(ev.peer)! : peerNode(ev.node, ev.peer, now);
        const e = edgeBetween(machine, other);
        const at = now - late;
        e.traffic += weight;
        e.last = Math.max(e.last, at);
        other.traffic += weight;
        other.last = Math.max(other.last, at);
        machine.traffic += weight;

        const cls = pulseClass(ev.cmd);
        mixLog.push({ at, cls });
        const grew = nodes.size + edges.size !== before;
        if (backlog) return grew;

        const from = ev.dir === "send" ? machine : other;
        const link = e.forward.source === from ? e.forward : e.back;
        pending.push({ link, next: { cls, label: LABELLED.has(ev.cmd) ? ev.cmd : null } });
        return grew;
      }

      frame();
      push();
      wrap.appendChild(tagLayer);

      // Set after the first graphData: that is when the inner simulation
      // exists. 3d-force-graph does not re-export the setter, so it is
      // reached on the inner graph object.
      const inner = graph
        .scene()
        .children.find(
          (c) => typeof (c as unknown as { d3AlphaTarget?: unknown }).d3AlphaTarget === "function",
        ) as unknown as { d3AlphaTarget: (a: number) => void } | undefined;
      inner?.d3AlphaTarget(0.03);

      let raf = 0;
      let prev = performance.now();
      let lastMix = 0;
      let frames = 0;
      let fpsFrom = prev;

      function loop(t: number) {
        const dt = Math.min(100, t - prev);
        prev = t;
        frameMs = 0.9 * frameMs + 0.1 * dt;
        const now = Date.now();

        const ready = pending;
        pending = [];
        for (const { link, next } of ready) {
          link.next = next;
          graph.emitParticle(link as never);
        }

        let grew = false;
        const head = player.playhead(now);
        for (const ev of player.drain(now)) grew = play(ev, now, head) || grew;

        const k = Math.exp(-dt / TRAFFIC_TAU_MS);
        let shrank = false;
        for (const [id, n] of nodes) {
          n.traffic *= k;
          if (!n.machine && now - n.last > PEER_GONE_MS) {
            nodes.delete(id);
            shrank = true;
          }
        }
        // Everything is sized against the traffic of the machine it belongs
        // to, never the total: one machine can carry five times what the other
        // does (inbound peers asking getaddr all day), and against the total
        // the quiet one draws as hairlines next to the loud one and reads as
        // switched off when it is not.
        const machineTotal = new Map<string, number>();
        for (const [key, e] of edges) {
          if (!nodes.has(e.forward.source.id) || !nodes.has(e.forward.target.id)) {
            edges.delete(key);
            e.material.dispose();
            continue;
          }
          e.traffic *= k;
          for (const m of edgeMachines(e)) machineTotal.set(m, (machineTotal.get(m) ?? 0) + e.traffic);
        }
        /** An edge's share of its machine's traffic; the direct edge takes the larger of two. */
        const shareOf = (e: GEdge) => {
          let best = 0;
          for (const m of edgeMachines(e)) {
            const t = machineTotal.get(m) ?? 0;
            if (t > 0) best = Math.max(best, e.traffic / t);
          }
          return best;
        };

        // Peers: radius by the largest share any machine gives them.
        const peerShare = new Map<string, number>();
        for (const e of edges.values()) {
          const sh = shareOf(e);
          for (const n of [e.forward.source, e.forward.target]) {
            if (!n.machine) peerShare.set(n.id, Math.max(peerShare.get(n.id) ?? 0, sh));
          }
        }
        for (const n of nodes.values()) {
          if (n.machine) continue;
          n.r = 4 + 12 * Math.sqrt(peerShare.get(n.id) ?? 0);
          if (n.__threeObj) {
            n.__threeObj.getObjectByName("body")?.scale.setScalar(n.r);
            n.__threeObj.getObjectByName("tag")?.position.set(0, -n.r - 12, 1);
          }
          // Nothing leaves the frame, labels included.
          const m = n.r + 24;
          if (n.x !== undefined) n.x = Math.max(-w / 2 + m, Math.min(w / 2 - m, n.x));
          if (n.y !== undefined) n.y = Math.max(-h / 2 + m, Math.min(h / 2 - m, n.y));
        }

        // Edges: width by share of their machine's traffic; brightness by how
        // recently they carried something, and how much, by the same share.
        for (const e of edges.values()) {
          const share = shareOf(e);
          const root = Math.sqrt(share);
          const glow = Math.exp(-(now - e.last) / EDGE_GLOW_MS);
          e.material.color.set(INK).multiplyScalar(EDGE_IDLE + EDGE_HOT * glow * (0.6 + 1.2 * root));
          // 1.6 is a floor, not a taste: the direct machine link is 1152 units
          // long and anything under about 1.5 is a sub-pixel sliver that the bloom
          // eats. The top end stays modest because these are solid meshes and the
          // fill cost is what the frame rate is made of — 0.6+4.5 halved it.
          e.width = 1.6 + 2.0 * root;
          {
          }
        }

        if (grew || shrank) push();

        // Pulse labels follow their particle until the library retires it.
        const cam = graph.camera();
        for (let i = tagged.length - 1; i >= 0; i--) {
          const { photon, el } = tagged[i];
          if (!photon.parent) {
            el.remove();
            tagged.splice(i, 1);
            continue;
          }
          // Placed on the next frame, once the library has moved it onto its link.
          if (!("__progressRatio" in photon)) {
            el.style.opacity = "0";
            continue;
          }
          el.style.opacity = "1";
          photon.getWorldPosition(projected).project(cam);
          const x = ((projected.x + 1) / 2) * w;
          const y = ((1 - projected.y) / 2) * h;
          el.style.transform = `translate(${(x + 9).toFixed(1)}px, ${(y - 22).toFixed(1)}px)`;
        }

        if (now - lastMix > 1000) {
          lastMix = now;
          while (mixLog.length && now - mixLog[0].at > MIX_WINDOW_MS) mixLog.shift();
          const mix: Mix = { discovery: 0, keepalive: 0, chain: 0, tx: 0 };
          for (const m of mixLog) mix[m.cls]++;
          cb.current.onMix?.(mix);
        }
        frames++;
        if (t - fpsFrom >= 1000) {
          cb.current.onFps?.((frames * 1000) / (t - fpsFrom));
          frames = 0;
          fpsFrom = t;
        }
        raf = requestAnimationFrame(loop);
      }
      raf = requestAnimationFrame(loop);

      const ro = new ResizeObserver(resize);
      ro.observe(wrap);

      cleanup = () => {
        cancelAnimationFrame(raf);
        ro.disconnect();
        graph.pauseAnimation();
        graph._destructor();
        wrap.replaceChildren();
      };
    })();

    return () => {
      disposed = true;
      cleanup();
    };
  }
}
