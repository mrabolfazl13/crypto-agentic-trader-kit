import type { Provenance } from "../api/client";
import Badge from "./Badge";
import { dateTime, text } from "../format";

/**
 * One-line provenance strip: which dataset window, which source, generated
 * when. Used under every chart and table so a simulated number can never be
 * read as a paper result.
 */
export default function ProvenanceNote({
  provenance,
  fallbackKind = null,
  extra,
}: {
  provenance: Provenance | null | undefined;
  fallbackKind?: Provenance["kind"] | null;
  extra?: string;
}) {
  const kind = provenance?.kind ?? fallbackKind ?? null;
  return (
    <p className="stat-meta" aria-label="منبع داده">
      <Badge kind={kind} />
      <span>پنجره: {text(provenance?.window)}</span>
      <span>منبع: {text(provenance?.source)}</span>
      <span>تولید: {dateTime(provenance?.generated_at)}</span>
      {extra ? <span>{extra}</span> : null}
    </p>
  );
}
