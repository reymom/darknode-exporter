/**
 * Colours for `/darknode/live`. Validated on the black stage surface with the
 * dataviz validator (dark band, CVD, normal-vision floor): each pair below
 * passes on its own. Re-run it before changing any of them:
 *
 *   node scripts/validate_palette.js "#2b9fd9,#d9489c,#7d828a,#e06c00" \
 *     --mode dark --surface "#000000"
 *
 * 2026-09-27, adding `tx` (#e06c00): it passes against each of the other three —
 * vs discovery ΔE 24.5 CVD / 30.1 normal, vs chain 16.7 / 19.6, vs keepalive
 * 13.6 / 19.0. Its one weak pair is tritan ΔE 4.9 against chain; tritanopia is
 * ~1 in 10,000 and every pulse carries its command as a direct label, which is
 * the secondary encoding that case requires. The grey's own failures (chroma
 * floor, deutan vs chain) are older than this change and deliberate: keepalive
 * is meant to recede.
 *
 * Two jobs, kept apart. A machine's colour is its identity — the ring in the
 * graph, the swatch over its column, its line on the memory chart. A pulse's
 * colour is what the message is for. Keepalive is a neutral grey on purpose:
 * it is maintenance, and it should recede behind the two that carry news.
 *
 * Text is never green here, unlike the rest of `/darknode`: green is one of
 * the machines on this page, so ink is neutral.
 */
import type { PulseClass } from "./node-pulse";

export const SURFACE = "#000000";
export const INK = "#e3e7ea";
export const INK_DIM = "#8b939c";
export const INK_FAINT = "#4a5057";
export const HAIRLINE = "#1f2328";
/** A block inside a block: one step off the surface. */
export const PLATE = "#0a0c0e";
/** Stale numbers. Always with the age in words next to it. */
export const AMBER = "#f5c542";

/**
 * One colour per machine, assigned in the order they are configured, so this
 * works for one machine or for six. The first two are the pair the palette was
 * validated on; the rest keep the same lightness band and chroma floor so a
 * third machine does not arrive looking like an error state.
 */
export const MACHINE_COLORS = [
  "#2fa51c",
  "#8f6cf0",
  "#d98c2b",
  "#2b9fd9",
  "#c94f7c",
  "#5fb8a6",
] as const;
export const MACHINE_FALLBACK = INK_DIM;

/** Stable per id: the same machine keeps its colour when another is added. */
export function machineColor(id: string, order: readonly string[]): string {
  const i = order.indexOf(id);
  return i < 0 ? MACHINE_FALLBACK : MACHINE_COLORS[i % MACHINE_COLORS.length];
}

/** Kept so existing callers that index by id still work. */
export const MACHINE_COLOR: Record<string, string> = {
  darknode: MACHINE_COLORS[0],
  "darkfi-obs": MACHINE_COLORS[1],
};

export const PULSE_COLOR: Record<PulseClass, string> = {
  discovery: "#2b9fd9",
  chain: "#d9489c",
  keepalive: "#7d828a",
  tx: "#e06c00",
};

export const PULSE_LABEL: Record<PulseClass, string> = {
  discovery: "discovery",
  keepalive: "keepalive",
  chain: "chain",
  tx: "transaction",
};
