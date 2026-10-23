import { NO_DATA, dateTime } from "../format";
import type { Bar } from "../api/client";
import { COLORS, PLOT, VIEW, bounds, gridLines, linePath, priceLabel, slotWidth, xIndex, yScale } from "./scale";

export interface Overlay {
  label: string;
  /** One value per rendered bar (null where the indicator is still warming up). */
  points: (number | null)[];
  color?: string;
}

/** Stop / target / entry reference lines, in price units. */
export interface Level {
  label: string;
  value: number | null | undefined;
  tone?: "stop" | "target" | "entry";
}

const TONE_COLOR: Record<string, string> = { stop: COLORS.neg, target: COLORS.pos, entry: COLORS.info };

/**
 * Candlesticks with EMA overlays and stop/target lines. Pure SVG on a fixed
 * viewBox so it scales with the card. With no bars it renders the no-data
 * message instead of an empty axis frame.
 */
export default function CandleChart({
  bars,
  overlays = [],
  levels = [],
  windowLabel,
  sourceLabel,
  totalBars,
}: {
  bars: Bar[] | null | undefined;
  overlays?: Overlay[];
  levels?: Level[];
  windowLabel?: string | null;
  sourceLabel?: string | null;
  totalBars?: number | null;
}) {
  const rendered = bars ?? [];
  if (rendered.length === 0) {
    return (
      <div className="chart">
        <div className="state" role="status">
          <h3>{NO_DATA}</h3>
          <p className="mute">کندلی برای ترسیم وجود ندارد؛ محورها هم رسم نمی‌شوند.</p>
        </div>
      </div>
    );
  }

  const priceValues: (number | null | undefined)[] = [];
  for (const bar of rendered) priceValues.push(bar.high, bar.low, bar.open, bar.close);
  for (const overlay of overlays) priceValues.push(...overlay.points);
  for (const level of levels) priceValues.push(level.value);
  const box = bounds(priceValues);
  if (!box) {
    return (
      <div className="chart">
        <div className="state" role="status">
          <h3>{NO_DATA}</h3>
          <p className="mute">مقادیر OHLC معتبر نیست.</p>
        </div>
      </div>
    );
  }

  const toY = yScale(box);
  const bodyWidth = slotWidth(rendered.length);
  const baseline = PLOT.y + PLOT.h;
  const tickEvery = Math.max(1, Math.floor(rendered.length / 5));

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${String(VIEW.w)} ${String(VIEW.h)}`} style={{ direction: "ltr" }} role="img" aria-label={`نمودار کندلی ${windowLabel ?? ""}`.trim()}>
        <title>{`کندل‌ها — ${windowLabel ?? NO_DATA}`}</title>
        {gridLines(box).map((value) => (
          <g key={`grid-${String(value)}`}>
            <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={toY(value)} y2={toY(value)} stroke={COLORS.grid} strokeWidth={1} />
            <text className="axis-label" x={PLOT.x - 6} y={toY(value) + 3} textAnchor="end">
              {priceLabel(value)}
            </text>
          </g>
        ))}
        <line x1={PLOT.x} x2={PLOT.x + PLOT.w} y1={baseline} y2={baseline} stroke={COLORS.grid} />

        {rendered.map((bar, index) => {
          const x = xIndex(index, rendered.length);
          const up = bar.close >= bar.open;
          const color = up ? COLORS.pos : COLORS.neg;
          const top = toY(Math.max(bar.open, bar.close));
          const bottom = toY(Math.min(bar.open, bar.close));
          return (
            <g key={`bar-${String(bar.t)}`}>
              <line x1={x} x2={x} y1={toY(bar.high)} y2={toY(bar.low)} stroke={color} strokeWidth={1} vectorEffect="non-scaling-stroke" />
              <rect
                x={x - bodyWidth / 2}
                y={top}
                width={Math.max(bodyWidth, 1)}
                height={Math.max(bottom - top, 0.8)}
                fill={color}
                opacity={up ? 0.85 : 0.95}
              >
                <title>{`${dateTime(bar.t)} — O ${priceLabel(bar.open)} H ${priceLabel(bar.high)} L ${priceLabel(bar.low)} C ${priceLabel(bar.close)}`}</title>
              </rect>
            </g>
          );
        })}

        {overlays.map((overlay, overlayIndex) => {
          const path = linePath(
            rendered.map((_, index) => {
              const value = overlay.points[index];
              return { x: xIndex(index, rendered.length), y: typeof value === "number" ? toY(value) : null };
            }),
          );
          const color = overlay.color ?? [COLORS.info, COLORS.warn, COLORS.accent][overlayIndex % 3] ?? COLORS.info;
          return path ? (
            <path key={`overlay-${overlay.label}`} d={path} fill="none" stroke={color} strokeWidth={1.6} vectorEffect="non-scaling-stroke" />
          ) : null;
        })}

        {levels.map((level) =>
          typeof level.value === "number" ? (
            <g key={`level-${level.label}`}>
              <line
                x1={PLOT.x}
                x2={PLOT.x + PLOT.w}
                y1={toY(level.value)}
                y2={toY(level.value)}
                stroke={TONE_COLOR[level.tone ?? "entry"]}
                strokeWidth={1.2}
                strokeDasharray="6 4"
                vectorEffect="non-scaling-stroke"
              />
              <text className="axis-label" x={PLOT.x + PLOT.w - 2} y={toY(level.value) - 4} textAnchor="end" fill={TONE_COLOR[level.tone ?? "entry"]}>
                {`${level.label} ${priceLabel(level.value)}`}
              </text>
            </g>
          ) : null,
        )}

        {rendered.map((bar, index) =>
          index % tickEvery === 0 ? (
            <text className="axis-label" key={`tick-${String(bar.t)}`} x={xIndex(index, rendered.length)} y={baseline + 14} textAnchor="middle">
              {dateTime(bar.t)}
            </text>
          ) : null,
        )}
      </svg>

      <div className="chart-legend">
        <span className="legend-key">
          <span className="legend-swatch" style={{ background: COLORS.pos }} />
          صعودی
        </span>
        <span className="legend-key">
          <span className="legend-swatch" style={{ background: COLORS.neg }} />
          نزولی
        </span>
        {overlays.map((overlay, index) => (
          <span className="legend-key" key={overlay.label}>
            <span
              className="legend-swatch"
              style={{ background: overlay.color ?? [COLORS.info, COLORS.warn, COLORS.accent][index % 3] ?? COLORS.info }}
            />
            {overlay.label}
          </span>
        ))}
        {levels.map((level) =>
          typeof level.value === "number" ? (
            <span className="legend-key" key={level.label}>
              <span className="legend-frame" style={{ borderColor: TONE_COLOR[level.tone ?? "entry"] }} />
              {`${level.label}: ${priceLabel(level.value)}`}
            </span>
          ) : null,
        )}
        <span className="mute">
          پنجره‌ی داده: {windowLabel ?? NO_DATA} · منبع: {sourceLabel ?? NO_DATA}
          {typeof totalBars === "number" && totalBars > rendered.length
            ? ` · نمایش ${String(rendered.length)} از ${String(totalBars)} کندل بسته`
            : null}
        </span>
      </div>
    </div>
  );
}
