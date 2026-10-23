/** Sidebar structure + route table for the RTL app shell. */

export interface NavItem {
  to: string;
  label: string;
  /** Short latin id used for aria/current matching and test hooks. */
  id: string;
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    title: "نمای کلی",
    items: [
      { to: "/", label: "داشبورد", id: "dashboard" },
      { to: "/portfolio", label: "پرتفوی", id: "portfolio" },
      { to: "/trades", label: "تاریخچه‌ی معاملات", id: "trades" },
    ],
  },
  {
    title: "داده و تحلیل",
    items: [
      { to: "/data", label: "داده‌های تاریخی", id: "data" },
      { to: "/analysis", label: "تحلیل بازار", id: "analysis" },
    ],
  },
  {
    title: "پژوهش استراتژی",
    items: [
      { to: "/strategies", label: "آزمایشگاه استراتژی", id: "strategies" },
      { to: "/backtests", label: "بک‌تست", id: "backtests" },
      { to: "/versions", label: "نسخه‌های استراتژی", id: "versions" },
    ],
  },
  {
    title: "اجرای کاغذی",
    items: [
      { to: "/paper", label: "پیپر تریدینگ", id: "paper" },
      { to: "/risk", label: "مدیریت ریسک", id: "risk" },
      { to: "/reports", label: "گزارش عملکرد", id: "reports" },
    ],
  },
  {
    title: "سامانه",
    items: [
      { to: "/logs", label: "لاگ‌های سیستم", id: "logs" },
      { to: "/settings", label: "تنظیمات", id: "settings" },
    ],
  },
];

export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((group) => group.items);

export function labelForPath(path: string): string {
  const match = NAV_ITEMS.find((item) => item.to === path);
  return match ? match.label : "کنسول پژوهش";
}

/** Standing copy required by the product rules; rendered in the shell footer. */
export const RESEARCH_DISCLAIMER =
  "این سامانه فقط پژوهش و حساب کاغذی است: هیچ کنترل «شروع معامله‌ی زنده» وجود ندارد و نخواهد داشت. بازده گذشته تضمین‌کننده‌ی آینده نیست و ممکن است تمام سرمایه‌ی اختصاص‌یافته از دست برود.";
