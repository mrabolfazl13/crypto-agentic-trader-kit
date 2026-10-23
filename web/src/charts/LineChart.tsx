import { NO_DATA, dateTime } from "../format";
import { COLORS, PLOT, VIEW, bounds, gridLines, linePath, priceLabel, timeTicks, xTime, yScale } from "./scale";
import type { XY } from "./scale";

export interface Series {
  id: string;
  label: string;
  /** [timestamp (ISO or epoch seconds), value] — null values leave a gap. */
  points: XY[];
  color?: string;
  dashed?: boolean;
}

/**
 * Multi-line chart on a shared time axis — used for the equity curve against
 * buy-and-hold. Empty input renders the no-data message, never an empty frame.
 */
export default function LineChart({
  series,
  windowLabel,
  sourceLabel,
  unit,
  ariaLabel = "نمودار خطی",
}: {
  series: Series[];
  windowLabel?: string | null;
  sourceLabel?: string | null;
  unit?: string;
  ariaLabel?: string;
}) {
  const usable = series.filter((s) => s.points.some(([, value]) => typeof value === "number" && Number.isFinite(value)));
  if (usable.length === 0) {
    return (
      <div className="chart">
        <div className="state" role="status">
          <h3>{NO_DATA}</h3>
          <p className="mute">سری داده‌ای برای ترسیم ارسال نشده است.</p>
        </div>
      </div>
    );
  }

  const allMillis: number[] = [];
  for (const s of usable) {
    for (const [t] of s.points) {
      const millis = typeof t === "number" ? (t < 1e11 ? t * 1000 : t) : Date.parse(t);
      if (Number.isFinite(millis)) allMillis.push(millis);
    }
  }
  const xBox = bounds(allMillis);
  const yBox = bounds(usable.flatMap((s) => s.points.map(([, value]) => (typeof value === "number" ? value : null))));
  if (!xBox || !yBox) {
    return (
      <div className="chart">
        <div className="state" role="status">
          <h3>{NO_DATA}</h3>
          <p className="mute">مقادیر نمودار معتبر نیستند.</p>
        </div>
      </div>
    );
  }

  const toX = xTime(xBox);
  const toY = yScale(yBox);
  const baseline = PLOT.y + PLOT.h;
  const palette = [COLORS.info, COLORS.warn, COLORS.accent, COLORS.pos];

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${String(VIEW.w)} ${String(VIEW.h)}`} style={{ direction: "ltr" }} role="img" aria-label={ariaLabel}>
        <title>{`${ariaLabel} — ${windowLabel ?? NO_DATA}`}</title>
        {gridLines(yBox).map((value) => (
          <g key={`g-${String(value)}`}>
            <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={toY(value)} y2={toY(value)} stroke={COLORS.grid} />
            <text className="axis-label" x={PLOT.x - 6} y={toY(value) + 3} textAnchor="end">
              {priceLabel(value)}
            </text>
          </g>
        ))}
        <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={baseline} y2={baseline} stroke={COLORS.grid} />

        {usable.map((s, index) => {
          const path = linePath(
            s.points.map(([t, value]) => {
              const millis = typeof t === "number" ? (t < 1e11 ? t * 1000 : t) : Date.parse(t);
              return { x: Number.isFinite(millis) ? toX(millis) : NaN, y: typeof value === "number" ? toY(value) : null };
            }),
          );
          return path ? (
            <path
              key={s.id}
              d={path}
              fill="none"
              stroke={s.color ?? palette[index % palette.length] ?? COLORS.info}
              strokeWidth={2}
              strokeDasharray={s.dashed ? "6 5" : undefined}
              vectorEffect="non-scaling-stroke"
            />
          ) : null;
        })}

        {timeTicks(xBox).map((tick, index) => (
          <text className="axis-label" key={`t-${String(index)}`} x={tick.x} y={baseline + 14} textAnchor="middle">
            {tick.label}
          </text>
        ))}
      </svg>

      <div className="chart-legend">
        {usable.map((s, index) => (
          <span className="legend-key" key={s.id}>
            <span className="legend-swatch" style={{ background: s.color ?? palette[index % palette.length] ?? COLORS.info }} />
            {s.label}
            <span className="mute">{lastValue(s, unit)}</span>
          </span>
        ))}
        <span className="mute">
          پنجره‌ی داده: {windowLabel ?? NO_DATA} · منبع: {sourceLabel ?? NO_DATA}
        </span>
      </div>
    </div>
  );
}

function lastValue(series: Series, unit?: string): string {
  for (let i = series.points.length - 1; i >= 0; i -= 1) {
    const point = series.points[i];
    if (point && typeof point[1] === "number") {
      const stamp = dateTime(point[0]);
      return `${point[1].toLocaleString("fa-IR-u-nu-latn", { maximumFractionDigits: 2 })}${unit ? ` ${unit}` : ""} (${stamp})`;
    }
  }
  return NO_DATA;
}
