import { NO_DATA, dateTime, plainPct } from "../format";
import { COLORS, PLOT, VIEW, areaPath, bounds, gridLines, priceLabel, timeTicks, xTime, yScale } from "./scale";
import type { XY } from "./scale";

/**
 * Drawdown area chart. Values come straight from the API
 * (`/equity-curve → [ts, equity, drawdown_pct]`) and are drawn below zero; no
 * synthetic trough is ever added when the series is short or missing.
 */
export default function DrawdownChart({
  points,
  windowLabel,
  sourceLabel,
}: {
  points: XY[];
  windowLabel?: string | null;
  sourceLabel?: string | null;
}) {
  const usable = points.filter(([, value]) => typeof value === "number" && Number.isFinite(value)) as [string | number, number][];
  if (usable.length < 2) {
    return (
      <div className="chart">
        <div className="state" role="status">
          <h3>{NO_DATA}</h3>
          <p className="mute">برای رسم افت سرمایه به کمتر از دو نقطه‌ی واقعی نیاز است.</p>
        </div>
      </div>
    );
  }

  const millis = usable.map(([t]) => (typeof t === "number" ? (t < 1e11 ? t * 1000 : t) : Date.parse(t)));
  const values = usable.map(([, value]) => Math.min(0, -Math.abs(value)));
  const xBox = bounds(millis);
  const yBox = bounds([...values, 0]);
  if (!xBox || !yBox) {
    return (
      <div className="chart">
        <div className="state" role="status">
          <h3>{NO_DATA}</h3>
          <p className="mute">مقادیر drawdown معتبر نیست.</p>
        </div>
      </div>
    );
  }

  const toX = xTime(xBox);
  const toY = yScale(yBox);
  const zero = toY(0);
  const coords = usable.map(([, value], index) => ({ x: toX(millis[index] ?? 0), y: toY(Math.min(0, -Math.abs(value))) }));
  const path = coords.map((p, index) => `${index === 0 ? "M" : "L"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  const area = areaPath(coords, zero);
  const worst = Math.min(...values);

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${String(VIEW.w)} ${String(VIEW.h)}`} style={{ direction: "ltr" }} role="img" aria-label="نمودار افت سرمایه">
        <title>{`افت سرمایه — ${windowLabel ?? NO_DATA}`}</title>
        {gridLines(yBox).map((value) => (
          <g key={`g-${String(value)}`}>
            <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={toY(value)} y2={toY(value)} stroke={COLORS.grid} />
            <text className="axis-label" x={PLOT.x - 6} y={toY(value) + 3} textAnchor="end">
              {`${priceLabel(value)}%`}
            </text>
          </g>
        ))}
        <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={zero} y2={zero} stroke={COLORS.neutral} strokeWidth={1} vectorEffect="non-scaling-stroke" />
        <path d={area} fill={COLORS.neg} opacity={0.22} />
        <path d={path} fill="none" stroke={COLORS.neg} strokeWidth={1.8} vectorEffect="non-scaling-stroke" />
        {timeTicks(xBox).map((tick, index) => (
          <text className="axis-label" key={`t-${String(index)}`} x={tick.x} y={PLOT.y + PLOT.h + 14} textAnchor="middle">
            {tick.label}
          </text>
        ))}
      </svg>
      <div className="chart-legend">
        <span className="legend-key">
          <span className="legend-swatch" style={{ background: COLORS.neg }} />
          افت از قله (منفی)
        </span>
        <span className="mute">بیشترین افت این پنجره: {plainPct(worst, { max: 2 })}</span>
        <span className="mute">
          پنجره‌ی داده: {windowLabel ?? NO_DATA} · منبع: {sourceLabel ?? NO_DATA}
        </span>
        <span className="mute">آخرین نقطه: {dateTime(usable[usable.length - 1]?.[0])}</span>
      </div>
    </div>
  );
}
