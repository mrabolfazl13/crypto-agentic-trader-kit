import { useId, useState } from "react";
import type { ReactNode } from "react";
import type { Provenance } from "../api/client";
import Badge from "./Badge";
import { NO_DATA, isAbsent } from "../format";

/** Small accessible tooltip: opens on hover and on keyboard focus. */
export function InfoTip({ text }: { text: string }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  return (
    <span className="tip">
      <button
        type="button"
        className="tip-trigger"
        aria-label={`توضیح: ${text}`}
        aria-describedby={open ? id : undefined}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen((v) => !v)}
      >
        ؟
      </button>
      <span className="tip-body" role="tooltip" id={id} hidden={!open}>
        {text}
      </span>
    </span>
  );
}

export interface StatProps {
  label: string;
  /** Already-formatted string, or null/undefined when the API sent no value. */
  value: ReactNode | null | undefined;
  tone?: "pos" | "neg" | "neutral";
  /** Exact window the number was computed over (never guessed). */
  window?: string | null;
  /** Which source produced it: gate / mexc / csv / engine. */
  source?: string | null;
  /** Which API path served it, e.g. `/portfolio`. */
  endpoint?: string;
  provenance?: Provenance | null;
  note?: string;
  /** Backend assumptions (e.g. the Sharpe caveat) shown in a tooltip. */
  tooltip?: string;
}

/**
 * A single metric with full provenance. If the value is absent it renders
 * `داده‌ای موجود نیست` — no zero, no sample number, ever.
 */
export default function Stat({ label, value, tone = "neutral", window, source, endpoint, provenance, note, tooltip }: StatProps) {
  const missing = value === null || value === undefined || (typeof value === "string" && isAbsent(value));
  return (
    <div className="card stat">
      <div className="stat-label">
        <span>{label}</span>
        {tooltip ? <InfoTip text={tooltip} /> : null}
      </div>
      <div className={`stat-value tone-${tone} ${missing ? "is-missing" : ""}`}>{missing ? NO_DATA : value}</div>
      <div className="stat-meta">
        {provenance ? <Badge kind={provenance.kind} /> : null}
        <span>پنجره: {window ? window : NO_DATA}</span>
        <span>منبع: {source ? source : NO_DATA}</span>
        {endpoint ? <span className="num">{endpoint}</span> : null}
      </div>
      {note ? <div className="stat-note">{note}</div> : null}
    </div>
  );
}
