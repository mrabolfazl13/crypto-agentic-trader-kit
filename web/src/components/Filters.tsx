import { useId } from "react";
import type { ReactNode } from "react";

export interface FilterField {
  key: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "select" | "date" | "number";
  options?: { value: string; label: string }[];
  placeholder?: string;
  hint?: string;
  disabled?: boolean;
}

/**
 * Filter bar for list pages. Every control is a labelled form field so it is
 * reachable by keyboard and announced correctly in RTL layout.
 */
export default function Filters({
  fields,
  actions,
  onReset,
  resultLabel,
}: {
  fields: FilterField[];
  actions?: ReactNode;
  onReset?: () => void;
  resultLabel?: ReactNode;
}) {
  const groupId = useId();
  return (
    <div className="filters" role="group" aria-labelledby={groupId}>
      <span id={groupId} className="sr-only">
        فیلترهای داده
      </span>
      {fields.map((field) => {
        const id = `${groupId}-${field.key}`;
        const type = field.type ?? "text";
        return (
          <div className="field" key={field.key}>
            <label htmlFor={id}>{field.label}</label>
            {type === "select" ? (
              <select id={id} value={field.value} onChange={(e) => field.onChange(e.target.value)} disabled={field.disabled}>
                {(field.options ?? []).map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                type={type}
                value={field.value}
                placeholder={field.placeholder}
                disabled={field.disabled}
                onChange={(e) => field.onChange(e.target.value)}
              />
            )}
            {field.hint ? <span className="mute">{field.hint}</span> : null}
          </div>
        );
      })}
      {resultLabel ? <span className="chip">{resultLabel}</span> : null}
      {onReset ? (
        <button type="button" className="btn ghost small" onClick={onReset}>
          پاک کردن فیلترها
        </button>
      ) : null}
      {actions ? <div className="btn-row">{actions}</div> : null}
    </div>
  );
}
