import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./StateBlocks";
import { NO_DATA, isAbsent } from "../format";

export interface Column<T> {
  key: string;
  header: string;
  /** Right-aligned monospace cell. */
  numeric?: boolean;
  /** Omit to make the column non-sortable. */
  sortValue?: (row: T) => string | number | null | undefined;
  render: (row: T, index: number) => ReactNode;
}

type Dir = "asc" | "desc";

export interface TableProps<T> {
  columns: Column<T>[];
  rows: T[];
  getRowId: (row: T, index: number) => string;
  caption?: string;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  emptyLabel?: string;
  emptyHint?: string;
  onRowClick?: (row: T) => void;
  /** Column key sorted by default (only used when the column is sortable). */
  defaultSort?: { key: string; dir: Dir };
}

/**
 * Sortable table with a sticky header and first-class loading / empty / error
 * states. Absent cell values render as `داده‌ای موجود نیست` rather than blank
 * or zero so gaps in the ledger stay visible.
 */
export default function Table<T>({
  columns,
  rows,
  getRowId,
  caption,
  loading,
  error,
  onRetry,
  emptyLabel,
  emptyHint,
  onRowClick,
  defaultSort,
}: TableProps<T>) {
  const [sort, setSort] = useState<{ key: string; dir: Dir } | null>(defaultSort ?? null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const column = columns.find((c) => c.key === sort.key);
    if (!column?.sortValue) return rows;
    const factor = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = column.sortValue?.(a);
      const bv = column.sortValue?.(b);
      // Missing values always lose against real ones, regardless of direction.
      if (isAbsent(av) && isAbsent(bv)) return 0;
      if (isAbsent(av)) return 1;
      if (isAbsent(bv)) return -1;
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * factor;
      return String(av).localeCompare(String(bv), "fa") * factor;
    });
  }, [rows, sort, columns]);

  if (loading) return <LoadingBlock label="در حال بارگذاری جدول…" />;
  if (error) return <ErrorBlock message={error} onRetry={onRetry} />;
  if (rows.length === 0) return <EmptyBlock label={emptyLabel ?? "موردی ثبت نشده است"} hint={emptyHint} />;

  function toggle(key: string) {
    setSort((current) => {
      if (current?.key !== key) return { key, dir: "desc" };
      return { key, dir: current.dir === "desc" ? "asc" : "desc" };
    });
  }

  return (
    <div className="table-wrap">
      <table className="data">
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => {
              const active = sort?.key === column.key;
              const ariaSort = column.sortValue ? (active ? (sort?.dir === "asc" ? "ascending" : "descending") : "none") : undefined;
              return (
                <th key={column.key} scope="col" className={column.numeric ? "num" : undefined} aria-sort={ariaSort}>
                  {column.sortValue ? (
                    <button type="button" className="sort-btn" onClick={() => toggle(column.key)} aria-label={`مرتب‌سازی بر اساس ${column.header}`}>
                      <span>{column.header}</span>
                      <span className="arrow" aria-hidden="true">
                        {active ? (sort?.dir === "asc" ? "▲" : "▼") : "↕"}
                      </span>
                    </button>
                  ) : (
                    <span>{column.header}</span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => (
            <tr
              key={getRowId(row, index)}
              data-clickable={onRowClick ? "true" : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              onKeyDown={
                onRowClick
                  ? (event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
            >
              {columns.map((column) => {
                const cell = column.render(row, index);
                const isEmptyCell = cell === null || cell === undefined || (typeof cell === "string" && isAbsent(cell));
                return (
                  <td key={column.key} className={column.numeric ? "num" : undefined}>
                    {isEmptyCell ? <span className="mute">{NO_DATA}</span> : cell}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
