import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { getHealth, getPortfolio, useAsync } from "./api/client";
import { InfoTip } from "./components/Stat";
import { NO_DATA, dateTime, money, num, sinceLabel, text } from "./format";
import { NAV_GROUPS, RESEARCH_DISCLAIMER, labelForPath } from "./nav";
import Analysis from "./pages/Analysis";
import Backtesting from "./pages/Backtesting";
import Dashboard from "./pages/Dashboard";
import Data from "./pages/Data";
import Logs from "./pages/Logs";
import PaperTrading from "./pages/PaperTrading";
import PerformanceReports from "./pages/PerformanceReports";
import Portfolio from "./pages/Portfolio";
import Risk from "./pages/Risk";
import Settings from "./pages/Settings";
import StrategyLab from "./pages/StrategyLab";
import Trades from "./pages/Trades";
import Versions from "./pages/Versions";

const COLLAPSE_KEY = "cat.sidebar.collapsed";
const THEME_KEY = "cat.theme";

type Theme = "dark" | "light";

function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage is a nicety, never a dependency */
  }
}

function Shell() {
  const [collapsed, setCollapsed] = useState<boolean>(() => readStored(COLLAPSE_KEY) === "1");
  const [theme, setTheme] = useState<Theme>(() => (readStored(THEME_KEY) === "light" ? "light" : "dark"));
  const [tick, setTick] = useState<number>(Date.now());
  const location = useLocation();
  const health = useAsync((signal) => getHealth(signal), []);
  const portfolio = useAsync((signal) => getPortfolio(signal), []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  // Refresh the "چند دقیقه پیش" labels without re-hitting the API.
  useEffect(() => {
    const timer = window.setInterval(() => setTick(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const toggleNav = useCallback(() => {
    setCollapsed((value) => {
      writeStored(COLLAPSE_KEY, value ? "0" : "1");
      return !value;
    });
  }, []);

  const kill = health.data?.kill_switch === true;
  // Two different facts: market-data freshness (newest stored candle) vs account refresh time.
  const dataStamp = health.data?.last_data_at ?? health.data?.generated_at ?? null;
  const accountStamp = portfolio.data?.updated_at ?? null;
  const lastCycle = health.data?.last_cycle_at ?? null;
  const modeRaw = health.data?.mode ?? null;
  const modeLabel = health.loading ? "…" : text(modeRaw === null ? null : String(modeRaw).toUpperCase());
  const summary = `حالت اجرا: ${text(modeRaw)} · توقف اضطراری: ${kill ? "فعال" : "خاموش"} · آخرین کندل ذخیره‌شده: ${dataStamp ? dateTime(dataStamp) : NO_DATA}`;
  const detailTip = [
    "منبع: /health و /portfolio",
    `حالت اجرا: ${text(modeRaw)} — این رابط هیچ کنترل «شروع معامله‌ی زنده» ندارد`,
    `توقف اضطراری (kill switch): ${kill ? "فعال — هیچ سفارش جدیدی ساخته نمی‌شود" : "خاموش، در دسترس"}`,
    `آخرین چرخه: ${lastCycle ? `${dateTime(lastCycle)} (${sinceLabel(lastCycle, tick)})` : NO_DATA}`,
    `آخرین به‌روزرسانی حساب: ${accountStamp ? `${dateTime(accountStamp)} (${sinceLabel(accountStamp, tick)})` : NO_DATA}`,
    `آخرین کندل ذخیره‌شده در Store: ${dataStamp ? `${dateTime(dataStamp)} (${sinceLabel(dataStamp, tick)})` : NO_DATA}`,
    `موقعیت‌های باز: ${num(portfolio.data?.positions?.length ?? null, { max: 0 })} · معامله‌ی امروز: ${num(portfolio.data?.trades_today ?? null, { max: 0 })} از ${num(portfolio.data?.max_trades_per_day ?? null, { max: 0 })}`,
    `کارمزد پرداختی: ${money(portfolio.data?.fees_paid ?? null)}`,
    portfolio.error ? `خطای /portfolio: ${portfolio.error}` : null,
    health.error ? `خطای /health: ${health.error}` : null,
  ]
    .filter((line): line is string => Boolean(line))
    .join("\n");

  return (
    <div className={collapsed ? "app is-collapsed" : "app"}>
      <a className="skip-link" href="#main-content">
        پریدن به محتوای اصلی
      </a>

      <aside className="sidebar" aria-label="راهنمای اصلی">
        <div className="brand">
          <span className="brand-title">کنسول پژوهش</span>
          <span className="brand-sub">PAPER / research only</span>
        </div>
        <button type="button" className="btn ghost small" onClick={toggleNav} aria-expanded={!collapsed} aria-controls="primary-nav">
          {collapsed ? "باز کردن منو" : "بستن منو"}
        </button>
        <nav id="primary-nav" aria-label="بخش‌ها">
          {NAV_GROUPS.map((group) => (
            <div key={group.title}>
              <p className="nav-group-title" id={`nav-group-${group.items[0]?.id ?? "x"}`}>
                {group.title}
              </p>
              <ul className="nav-list" aria-labelledby={`nav-group-${group.items[0]?.id ?? "x"}`}>
                {group.items.map((item) => (
                  <li key={item.id}>
                    <NavLink to={item.to} end={item.to === "/"} className="nav-link" title={item.label}>
                      <span aria-hidden="true" className="dot" />
                      <span className="nav-label">{item.label}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
      </aside>

      <div className="main">
        <header className="topbar">
          <h1>{labelForPath(location.pathname)}</h1>
          <div className="spacer" />
          <div className="btn-row" role="status" aria-live="polite" aria-label="خلاصه‌ی وضعیت سرویس">
            <span className="chip" title={summary}>
              <span aria-hidden="true" className={`dot ${kill ? "danger pulse" : "ok"}`} />
              <strong>{modeLabel}</strong>
              <span className="mute">·</span>
              <span>{kill ? "توقف اضطراری فعال" : "توقف اضطراری خاموش"}</span>
              <span className="mute">·</span>
              <span>{`داده: ${dataStamp ? sinceLabel(dataStamp, tick) : NO_DATA}`}</span>
            </span>
            <InfoTip text={detailTip} />
            {health.error ? (
              <span className="chip" title={health.error}>
                <span aria-hidden="true" className="dot warn" />
                سرور در دسترس نیست
              </span>
            ) : null}
            <button
              type="button"
              className="btn ghost small"
              onClick={() => {
                setTheme(theme === "dark" ? "light" : "dark");
                writeStored(THEME_KEY, theme === "dark" ? "light" : "dark");
              }}
              aria-label={theme === "dark" ? "روشن کردن حالت روشن" : "روشن کردن حالت تیره"}
            >
              {theme === "dark" ? "روشن" : "تیره"}
            </button>
            <button type="button" className="btn small" onClick={health.reload} aria-label="تازه‌سازی وضعیت سرویس">
              تازه‌سازی
            </button>
          </div>
        </header>

        <main className="content" id="main-content" tabIndex={-1}>
          <Routes>
            <Route index element={<Dashboard />} />
            <Route path="/data" element={<Data />} />
            <Route path="/analysis" element={<Analysis />} />
            <Route path="/strategies" element={<StrategyLab />} />
            <Route path="/backtests" element={<Backtesting />} />
            <Route path="/paper" element={<PaperTrading />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/risk" element={<Risk />} />
            <Route path="/reports" element={<PerformanceReports />} />
            <Route path="/versions" element={<Versions />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Dashboard />} />
          </Routes>
          <footer className="notice warn">{RESEARCH_DISCLAIMER}</footer>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}
