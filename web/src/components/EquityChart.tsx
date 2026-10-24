import { dateTime, signed } from "../format";

export interface EquitySeries {
  label: string;
  color: string;
  points: [number, number][];
  dashed?: boolean;
}

const WIDTH = 560;
const HEIGHT = 180;
const PAD = { top: 10, right: 12, bottom: 20, left: 12 };

/**
 * Small dependency-free SVG line chart. Values are percent of initial capital, so the
 * dashed 100 line is "no change"; every line ends where its net return says it does.
 */
export default function EquityChart({ series, height = HEIGHT }: { series: EquitySeries[]; height?: number }) {
  const usable = series.filter((line) => line.points.length >= 2);
  if (!usable.length) return null;
  const times = usable.flatMap((line) => line.points.map((point) => point[0]));
  const values = usable.flatMap((line) => line.points.map((point) => point[1]));
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const lo = Math.min(...values, 100.0);
  const hi = Math.max(...values, 100.0);
  const pad = Math.max((hi - lo) * 0.06, 0.25);
  const minV = lo - pad;
  const maxV = hi + pad;
  const x = (t: number) => PAD.left + ((t - minT) / (maxT - minT || 1)) * (WIDTH - PAD.left - PAD.right);
  const y = (v: number) => height - PAD.bottom - ((v - minV) / (maxV - minV || 1)) * (height - PAD.top - PAD.bottom);
  const [firstLine] = usable;
  if (!firstLine) return null;
  const first = firstLine.points[0]?.[0] ?? 0;
  const last = firstLine.points[firstLine.points.length - 1]?.[0] ?? 0;
  return (
    <figure style={{ margin: 0 }}>
      <div dir="ltr">
        <svg viewBox={`0 0 ${WIDTH} ${height}`} style={{ width: "100%", height: "auto" }} role="img"
             aria-label="منحنی سرمایه خارج از نمونه">
          {100 >= minV && 100 <= maxV ? (
            <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(100)} y2={y(100)}
                  stroke="currentColor" strokeDasharray="3 4" opacity="0.3" />
          ) : null}
          {usable.map((line) => (
            <polyline
              key={line.label}
              fill="none"
              stroke={line.color}
              strokeWidth={line.dashed ? 1.5 : 2}
              strokeDasharray={line.dashed ? "5 4" : undefined}
              strokeLinejoin="round"
              strokeLinecap="round"
              points={line.points.map((point) => `${x(point[0]).toFixed(2)},${y(point[1]).toFixed(2)}`).join(" ")}
            />
          ))}
        </svg>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, opacity: 0.7 }}>
          <span>{dateTime(first)}</span>
          <span>{dateTime(last)}</span>
        </div>
      </div>
      <figcaption style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 6, fontSize: 12 }}>
        {usable.map((line) => {
          const end = line.points[line.points.length - 1]?.[1] ?? 100;
          return (
            <span key={line.label} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span aria-hidden style={{ width: 14, height: 3, background: line.color, borderRadius: 2, display: "inline-block" }} />
              {line.label}: <b>{signed(end - 100, { max: 2 })}%</b>
            </span>
          );
        })}
      </figcaption>
    </figure>
  );
}
