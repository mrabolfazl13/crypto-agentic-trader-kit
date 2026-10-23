import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes, useLocation } from "react-router-dom";
import {
  CandlestickChart,
  ChartLine,
  Database,
  FileText,
  FlaskConical,
  GitBranch,
  History,
  LayoutDashboard,
  Landmark,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  PlayCircle,
  ReceiptText,
  RefreshCw,
  ScrollText,
  Settings,
  ShieldCheck,
  Sun,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { getHealth, getPortfolio, useAsync } from "./api/client";
import { InfoTip } from "./components/Stat";
import { Button } from "./components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "./components/ui/tooltip";
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
import SettingsPage from "./pages/Settings";
import StrategyLab from "./pages/StrategyLab";
import Trades from "./pages/Trades";
import TseMarket from "./pages/tse/TseMarket";
import Versions from "./pages/Versions";

const COLLAPSE_KEY = "cat.sidebar.collapsed";
const THEME_KEY = "cat.theme";

type Theme = "dark" | "light";

const NAV_ICONS: Record<string, LucideIcon> = {
  dashboard: LayoutDashboard,
  portfolio: Wallet,
  trades: ReceiptText,
  data: Database,
  analysis: ChartLine,
  strategies: FlaskConical,
  backtests: History,
  versions: GitBranch,
  paper: PlayCircle,
  risk: ShieldCheck,
  reports: FileText,
  logs: ScrollText,
  settings: Settings,
  tse: Landmark,
};

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
          <span className="brand-mark" aria-hidden="true">
            <CandlestickChart size={20} />
          </span>
          <div className="brand-text">
            <span className="brand-title">کنسول پژوهش</span>
            <span className="brand-sub">CAT · PAPER ONLY</span>
          </div>
        </div>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="sm" onClick={toggleNav} aria-expanded={!collapsed} aria-controls="primary-nav" className="justify-start">
              {collapsed ? <PanelLeftOpen size={16} aria-hidden="true" /> : <PanelLeftClose size={16} aria-hidden="true" />}
              <span className={collapsed ? "sr-only" : ""}>{collapsed ? "باز کردن منو" : "بستن منو"}</span>
            </Button>
          </TooltipTrigger>
          {collapsed ? <TooltipContent side="left">نمایش منو</TooltipContent> : null}
        </Tooltip>

        <nav id="primary-nav" aria-label="بخش‌ها">
          {NAV_GROUPS.map((group) => (
            <div key={group.title}>
              <p className="nav-group-title" id={`nav-group-${group.items[0]?.id ?? "x"}`}>
                {group.title}
              </p>
              <ul className="nav-list" aria-labelledby={`nav-group-${group.items[0]?.id ?? "x"}`}>
                {group.items.map((item) => {
                  const Icon = NAV_ICONS[item.id] ?? CandlestickChart;
                  const link = (
                    <NavLink to={item.to} end={item.to === "/"} className="nav-link" title={item.label} aria-label={item.label}>
                      <Icon className="nav-icon" size={17} aria-hidden="true" />
                      <span className="nav-label">{item.label}</span>
                    </NavLink>
                  );
                  return (
                    <li key={item.id}>
                      {collapsed ? (
                        <Tooltip>
                          <TooltipTrigger asChild>{link}</TooltipTrigger>
                          <TooltipContent side="left">{item.label}</TooltipContent>
                        </Tooltip>
                      ) : (
                        link
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <span className="inline-flex items-center gap-1.5">
            <ShieldCheck size={12} aria-hidden="true" /> حساب کاغذی — بدون سفارش زنده
          </span>
          <span>داده‌ی بازار: منبع محلی Store · بدون اتصال به صرافی در UI</span>
        </div>
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
              <span>{kill ? "توقف اضطراری: فعال" : "توقف: خاموش"}</span>
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
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => {
                    setTheme(theme === "dark" ? "light" : "dark");
                    writeStored(THEME_KEY, theme === "dark" ? "light" : "dark");
                  }}
                  aria-label={theme === "dark" ? "روشن کردن حالت روشن" : "روشن کردن حالت تیره"}
                >
                  {theme === "dark" ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">{theme === "dark" ? "حالت روشن" : "حالت تیره"}</TooltipContent>
            </Tooltip>
            <Button variant="default" size="sm" onClick={health.reload} aria-label="تازه‌سازی وضعیت سرویس">
              <RefreshCw size={14} aria-hidden="true" />
              تازه‌سازی
            </Button>
          </div>
        </header>

        <main className="content" id="main-content" tabIndex={-1}>
          <Routes>
            <Route index element={<Dashboard />} />
            <Route path="/data" element={<Data />} />
            <Route path="/analysis" element={<Analysis />} />
            <Route path="/tse" element={<TseMarket />} />
            <Route path="/strategies" element={<StrategyLab />} />
            <Route path="/backtests" element={<Backtesting />} />
            <Route path="/paper" element={<PaperTrading />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/risk" element={<Risk />} />
            <Route path="/reports" element={<PerformanceReports />} />
            <Route path="/versions" element={<Versions />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/settings" element={<SettingsPage />} />
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
    <TooltipProvider delayDuration={200}>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </TooltipProvider>
  );
}
