import type { ReactNode } from "react";
import type { ProvenanceKind } from "../api/client";

/** Persian label for each provenance kind in the contract. */
const KIND_LABEL: Record<ProvenanceKind, string> = {
  history: "تاریخچه",
  simulated: "شبیه‌سازی",
  paper: "پیپر",
  live: "زنده",
};

export function provenanceLabel(kind: ProvenanceKind | null | undefined): string {
  if (!kind) return "نامشخص";
  return KIND_LABEL[kind] ?? String(kind);
}

/**
 * Provenance badge. Simulated backtest numbers, paper results and raw history
 * must never look alike, so each kind gets its own colour and its own words.
 */
export default function Badge({
  kind,
  source,
  title,
  label,
}: {
  kind: ProvenanceKind | null | undefined;
  source?: string | null;
  title?: string;
  label?: string;
}) {
  const known = kind === "history" || kind === "simulated" || kind === "paper" || kind === "live";
  return (
    <span className={`badge ${known ? `kind-${String(kind)}` : "tone-neutral"}`} title={title}>
      <span aria-hidden="true" className="dot" />
      {label ?? provenanceLabel(kind)}
      {source ? <span className="mute">· {source}</span> : null}
    </span>
  );
}

export type Tone = "pos" | "neg" | "warn" | "neutral" | "info";

/** Non-provenance status badge (approved/rejected, healthy/error, …). */
export function ToneBadge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge tone-${tone}`} title={title}>
      {children}
    </span>
  );
}

/** Mode chip — the UI shows the mode it read from /health; it never edits it. */
export function ModeBadge({ mode }: { mode: string | null | undefined }) {
  const upper = (mode ?? "").toUpperCase();
  const label = upper === "PAPER" ? "پیپر" : upper === "LIVE" ? "زنده" : upper === "RESEARCH" ? "پژوهش" : upper === "PAUSED" ? "متوقف" : "نامشخص";
  const kind: ProvenanceKind = upper === "LIVE" ? "live" : upper === "PAPER" ? "paper" : "simulated";
  return <Badge kind={kind} label={label} title={`حالت اجرا از /health: ${mode ?? "داده‌ای موجود نیست"}`} />;
}
