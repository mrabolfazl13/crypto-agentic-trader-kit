# معماری پلتفرم تحقیق و بهینه‌سازی

این سند مرزهای مسئولیت و قواعد داده را قفل می‌کند تا هیچ لایه‌ای نتواند عددی را بدون منشأ ارائه دهد.

## جریان داده
```
Gate.io / MEXC / CSV
   → datasets (parse + validate + quality)      [origin: exchange|csv]
   → store (SQLite: datasets, bars, runs, …)
   → indicators / strategy (Analysis, Proposal)  [params ⇒ StrategyParams]
   → Risk Gate (قطعی، بدون تصمیم‌گیری زبانی)
   → broker (Paper | Live-blocked)
   → store (decisions, orders, fills, trades, portfolio_snapshots)
   → research (split → grid(train) → validation → walk-forward → acceptance)
   → api (FastAPI, loopback)  →  web (Vite+React, RTL)  →  گزارش روزانه
```

## لایه‌ها و مالکیت فایل
| لایه | فایل | مسئولیت | مجاز به نوشتن |
|---|---|---|---|
| Config / gate | `src/cat/config.py` | قفل حالت PAPER، digest، رد برداشت/اهرم | — |
| داده | `src/cat/data.py`, `src/cat/datasets.py` | OHLCV، اعتبارسنجی، کیفیت، resample | `datasets` |
| اندیکاتور | `src/cat/indicators.py` | SMA/EMA/RSI/ATR/vol/slope | — |
| استراتژی | `src/cat/strategy.py`, `src/cat/strategies.py` | تحلیل + Proposal؛ بدون حق سفارش | — |
| ریسک | `src/cat/risk.py` | تنها لایه‌ی تعیین اندازه/مجاز‌سازی | — |
| اجرا | `src/cat/broker.py`, `src/cat/engine.py` | Paper؛ `LiveExecutor` بدون adapter | `state/*.json` |
| ممیزی | `src/cat/audit.py` | زنجیره‌ی hash + redaction | `state/audit` |
| Persistence | `src/cat/store.py` | SQLite، ستون `mode/origin/source` الزامی | `state/platform.sqlite3` |
| تحقیق | `src/cat/research.py` | split/grid/walk-forward/acceptance | `runs`, `strategy_versions` |
| API | `src/cat/api.py`, `serve.py` | خواندن + اجرای تحقیق/paper | — |
| UI | `web/` | نمایش با برچسب منشأ | — |

دو ایجنت هرگز یک فایل مشترک را هم‌زمان تغییر نمی‌دهند؛ قراردادها در `docs/API_CONTRACT_FA.md` و این جدول قفل شده‌اند.

## قواعد منشأ عدد (Non-negotiable)
1. هر عدد باید `kind` داشته باشد: `history` (داده‌ی خام)، `simulated` (بک‌تست)، `paper` (حساب شبیه‌سازی‌شده)، `live` (در این نسخه وجود ندارد).
2. نبود داده ⇒ `null` و پیام «داده‌ای موجود نیست». هیچ مقدار نمونه‌ای، پیش‌فرض خوش‌بینانه یا عدد رند جای داده واقعی نمی‌آید.
3. بک‌تست با `assumed_spread_pct` ثابت است؛ این فرض باید کنار عدد نمایش داده شود چون داده‌ی عمق تاریخ در دسترس نیست.
4. Sharpe/Sortino خام‌اند (بدون نرخ ریسک‌free، فرض iid) و برچسبشان حذف نمی‌شود.
5. قیمت اجرای سفارش هرگز برابر قیمت مطلوب فرض نمی‌شود: تصمیم روی کندل t، اجرا روی باز شدن t+1 با slippage؛ برخورد هم‌زمان stop و target ⇒ stop-first (بدبینانه).

## حلقه‌ی بهبود مستمر و ضد-overfitting
چرخه: تحلیل عملکرد ⇒ شناسایی علت زیان ⇒ محدودیت‌های نسخه ⇒ ساخت پارامتر جدید ⇒ بک‌تست روی train ⇒ امتیاز روی validation ⇒ walk-forward ⇒ مقایسه با champion ⇒ ثبت پذیرش/رد (شکست‌ها هم ثبت می‌شوند).

قوانین پذیرش نسخه‌ی جدید (در `research.acceptance` enforce می‌شود، نه در prompt):
- نتیجه روی داده‌ی خارج از نمونه اندازه‌گیری شده باشد.
- هزینه‌های رفت‌وبرگشت در نتایج لحاظ شده باشد.
- بازده خالص و profit factor هر دو روی داده خارج از نمونه بهتر از champion.
- max drawdown بدتر از champion بیش از ۱ واحد درصد نباشد.
- حداقل تعداد معاملات (`min_trades`) انجام شده باشد تا آماری معنادار باشد.
- در حداقل نیمی از foldها سودآور باشد (یک دوره‌ی خوش‌شانس نمی‌تواند نسخه بسازد).
- هیچ سقف ریسکی افزایش نیافته باشد.
در غیر این صورت champion می‌ماند و دلیل رد در `strategy_versions.decision` ثبت می‌شود.

## حالت‌ها
- `research`: تحلیل و بک‌تست، بدون هیچ سفارشی.
- `paper`: حالت پیش‌فرض فعلی؛ موجودی فرضی، قیمت واقعی، ثبت کامل.
- `live`: در API و UI وجود ندارد. فعال‌سازی فقط با سه شرط مستقل در فایل پیکربندی و رکورد تأیید مالک که به digest همان پیکربندی گره خورده است؛ حتی آن‌گاه adapter زنده وصل نیست.
