/**
 * Shared maths for the hand-rolled SVG charts (no chart library).
 *
 * Charts keep the conventional left-to-right time axis inside the SVG
 * (`dir="ltr"`) while every label, legend and tooltip stays Persian/RTL —
 * mirroring a time axis reads as a bug to traders.
 */

import { dateOnly, num } from "../format";

/** Plot box in viewBox units; every chart renders into 800x300. */
export const VIEW = { w: 800, h: 300 } as const;
export const PAD = { left: 58, right: 10, top: 12, bottom: 28 } as const;
export const PLOT = {
  x: PAD.left,
  y: PAD.top,
  w: VIEW.w - PAD.left - PAD.right,
  h: VIEW.h - PAD.top - PAD.bottom,
} as const;

export type XY = [string | number, number | null];

export interface Bounds {
  min: number;
  max: number;
}

export function toMillis(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return Number.isFinite(value) ? (value < 1e11 ? value * 1000 : value) : null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

export function bounds(values: Iterable<number | null | undefined>): Bounds | null {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const value of values) {
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    if (value < min) min = value;
    if (value > max) max = value;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  if (min === max) {
    const pad = Math.abs(min) * 0.02 || 1;
    return { min: min - pad, max: max + pad };
  }
  return { min, max: max + (max - min) * 0.04 };
}

export function yScale(b: Bounds): (value: number) => number {
  const span = b.max - b.min || 1;
  return (value: number) => PLOT.y + PLOT.h - ((value - b.min) / span) * PLOT.h;
}

/** Index-based x (candles): each bar owns an equal slot. */
export function xIndex(index: number, count: number): number {
  const slot = PLOT.w / Math.max(count, 1);
  return PLOT.x + slot * index + slot / 2;
}

export function slotWidth(count: number): number {
  return (PLOT.w / Math.max(count, 1)) * 0.62;
}

/** Time-based x (line/area charts). */
export function xTime(domain: Bounds): (millis: number) => number {
  const span = domain.max - domain.min || 1;
  return (millis: number) => PLOT.x + ((millis - domain.min) / span) * PLOT.w;
}

export function gridLines(b: Bounds, steps = 4): number[] {
  const out: number[] = [];
  for (let i = 0; i <= steps; i += 1) out.push(b.min + ((b.max - b.min) * i) / steps);
  return out;
}

/** Evenly spaced x ticks derived from the real data domain. */
export function timeTicks(domain: Bounds, count = 4): { x: number; label: string }[] {
  const toX = xTime(domain);
  const ticks: { x: number; label: string }[] = [];
  for (let i = 0; i < count; i += 1) {
    const millis = domain.min + ((domain.max - domain.min) * i) / (count - 1 || 1);
    ticks.push({ x: toX(millis), label: dateOnly(millis) });
  }
  return ticks;
}

export function priceLabel(value: number): string {
  const decimals = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 1 ? 2 : 4;
  return num(value, { max: decimals, min: decimals });
}

/** Builds an SVG path, skipping gaps instead of drawing across them. */
export function linePath(points: { x: number; y: number | null }[]): string {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (p.y === null || !Number.isFinite(p.y)) {
      pen = false;
      continue;
    }
    d += `${pen ? " L" : " M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`;
    pen = true;
  }
  return d.trim();
}

export function areaPath(points: { x: number; y: number }[], baseline: number): string {
  if (points.length === 0) return "";
  const head = `M${points[0]?.x.toFixed(1)} ${baseline.toFixed(1)}`;
  const body = points.map((p) => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  const tail = `L${points[points.length - 1]?.x.toFixed(1)} ${baseline.toFixed(1)} Z`;
  return `${head} ${body} ${tail}`;
}

export const COLORS = {
  pos: "var(--pos)",
  neg: "var(--neg)",
  warn: "var(--warn)",
  info: "var(--info)",
  accent: "var(--accent)",
  grid: "var(--grid)",
  dim: "var(--text-mute)",
  neutral: "var(--text-dim)",
} as const;
