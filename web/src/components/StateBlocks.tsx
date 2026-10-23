import type { ReactNode } from "react";
import type { AsyncState } from "../api/client";
import { NO_DATA } from "../format";

export function LoadingBlock({ label = "در حال بارگذاری…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <h3>{label}</h3>
      <p className="mute">تا رسیدن پاسخ سرور، هیچ عددی نمایش داده نمی‌شود.</p>
    </div>
  );
}

export function EmptyBlock({
  label = NO_DATA,
  hint = "برای دیدن این بخش، ابتدا یک مجموعه‌داده یا چرخه‌ی پیپر ثبت کنید.",
  actions,
}: {
  label?: string;
  hint?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="state" role="status" aria-live="polite">
      <h3>{label}</h3>
      <p>{hint}</p>
      {actions ? <div className="btn-row">{actions}</div> : null}
    </div>
  );
}

export function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state error" role="alert" aria-live="assertive">
      <h3>دریافت داده ممکن نشد</h3>
      <p>{message}</p>
      <p className="mute">سیستم در حالت امن است؛ هیچ سفارشی ساخته یا تغییر نمی‌کند.</p>
      {onRetry ? (
        <div className="btn-row">
          <button type="button" className="btn small" onClick={onRetry}>
            تلاش دوباره
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function SuccessBlock({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state success" role="status" aria-live="polite">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

/** Standing note for pages the data layer has not been wired into yet. */
export function TodoData({ endpoints }: { endpoints: string[] }) {
  return (
    <div className="notice warn">
      <strong>TODO_DATA</strong>
      <span>
        این صفحه هنوز به داده وصل نشده است. منابع مورد نیاز:{" "}
        <code>{endpoints.join("، ")}</code>
      </span>
    </div>
  );
}

/**
 * Renders exactly one of loading / error / empty / content for an
 * `useAsync` result. Content only ever sees real API data.
 */
export default function StateBlocks<T>({
  state,
  children,
  emptyLabel,
  emptyHint,
  loadingLabel,
}: {
  state: AsyncState<T>;
  children: (data: T) => ReactNode;
  emptyLabel?: string;
  emptyHint?: string;
  loadingLabel?: string;
}) {
  if (state.loading) return <LoadingBlock label={loadingLabel} />;
  if (state.error) return <ErrorBlock message={state.error} onRetry={state.reload} />;
  if (state.isEmpty || state.data === null) return <EmptyBlock label={emptyLabel} hint={emptyHint} />;
  return <>{children(state.data)}</>;
}
