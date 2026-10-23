import { NO_DATA, pct } from "../format";
import { COLORS, PLOT, VIEW, bounds, gridLines, yScale } from "./scale";

export interface FoldBar {
  label: string;
  /** Out-of-sample return of this fold, in percent. Null stays null. */
  value: number | null;
  window?: string | null;
}

/**
 * Per-fold out-of-sample returns as bars around a zero line. Folds with no
 * recorded result appear as a dashed slot labelled `داده‌ای موجود نیست`, never
 * as a zero-length (i.e. "flat") bar — a missing fold is not a break-even fold.
 */
export default function MetricBars({
  items,
  windowLabel,
  sourceLabel,
  ariaLabel = "بازده هر پنجره‌ی آزمون برون‌نمونه‌ای",
}: {
  items: FoldBar[];
  windowLabel?: string | null;
  sourceLabel?: string | null;
  ariaLabel?: string;
}) {
  const numeric = items.map((item) => item.value).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (items.length === 0 || numeric.length === 0) {
    return (
      <div className="chart">
        <div className="state" role="status">
          <h3>{NO_DATA}</h3>
          <p className="mute">نتیجه‌ای از پنجره‌های آزمون ثبت نشده است.</p>
        </div>
      </div>
    );
  }

  const box = bounds([...numeric, 0]);
  if (!box) return null;
  const toY = yScale(box);
  const zero = toY(0);
  const slot = PLOT.w / items.length;
  const barWidth = Math.max(slot * 0.55, 6);

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${String(VIEW.w)} ${String(VIEW.h)}`} style={{ direction: "ltr" }} role="img" aria-label={ariaLabel}>
        <title>{`${ariaLabel} — ${windowLabel ?? NO_DATA}`}</title>
        {gridLines(box, 4).map((value) => (
          <g key={`g-${String(value)}`}>
            <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={toY(value)} y2={toY(value)} stroke={COLORS.grid} />
            <text className="axis-label" x={PLOT.x - 6} y={toY(value) + 3} textAnchor="end">
              {`${value.toFixed(1)}%`}
            </text>
          </g>
        ))}
        <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={zero} y2={zero} stroke={COLORS.neutral} vectorEffect="non-scaling-stroke" />

        {items.map((item, index) => {
          const centre = PLOT.x + slot * index + slot / 2;
          const labelY = PLOT.y + PLOT.h + 14;
          if (typeof item.value !== "number" || !Number.isFinite(item.value)) {
            return (
              <g key={item.label}>
                <line
                  x1={centre}
                  x2={centre}
                  y1={PLOT.y + 6}
                  y2={PLOT.y + PLOT.h - 6}
                  stroke={COLORS.dim}
                  strokeDasharray="4 4"
                  vectorEffect="non-scaling-stroke"
                />
                <text className="axis-label" x={centre} y={labelY} textAnchor="middle">
                  {item.label}
                </text>
              </g>
            );
          }
          const y = toY(item.value);
          const top = item.value >= 0 ? y : zero;
          const height = Math.max(Math.abs(zero - y), 1);
          const color = item.value >= 0 ? COLORS.pos : COLORS.neg;
          return (
            <g key={item.label}>
              <rect x={centre - barWidth / 2} y={top} width={barWidth} height={height} fill={color} opacity={0.85} rx={2}>
                <title>{`${item.label}${item.window ? ` — ${item.window}` : ""}: ${pct(item.value, { max: 2 })}`}</title>
              </rect>
              <text className="axis-label" x={centre} y={item.value >= 0 ? top - 4 : top + height + 10} textAnchor="middle">
                {pct(item.value, { max: 1 })}
              </text>
              <text className="axis-label" x={centre} y={labelY} textAnchor="middle">
                {item.label}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="chart-legend">
        <span className="legend-key">
          <span className="legend-swatch" style={{ background: COLORS.pos }} />
          بازده مثبت
        </span>
        <span className="legend-key">
          <span className="legend-swatch" style={{ background: COLORS.neg }} />
          بازده منفی
        </span>
        <span className="mute">خط‌چین = پنجره‌ای بدون نتیجه</span>
        <span className="mute">
          پنجره‌ی داده: {windowLabel ?? NO_DATA} · منبع: {sourceLabel ?? NO_DATA}
        </span>
      </div>
    </div>
  );
}
