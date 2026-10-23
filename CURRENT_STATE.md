# CURRENT_STATE

به‌روزرسانی: 2026-10-23T09:30Z (UTC) — هر ۱۳ صفحه نوشته و build شد؛ دو باگ قراردادی walk-forward رفع و روی سرور واقعی راستی‌آزمایی شد.

## لایه‌هایی که الان کار می‌کنند (راستی‌آزمایی‌شده با اجرا)
| لایه | وضعیت | شواهد (همه اجراشده، نه ادعا) |
|---|---|---|
| داده‌ی بازار واقعی | سبز | `python trader.py check` → `check: PASS`؛ `fetch-history` → `ds-ex-43cf0e785f` (BTC 4h، ۲۱۹۰ کندل بسته، coverage 100%، صفر شکاف/تکرار) و `ds-ex-0f36cb81e7` (ETH 4h، ۲۱۹۰ کندل)؛ gateio/mexc از طریق پروکسی 200 OK |
| اعتبارسنجی داده | سبز | `tests/test_datasets.py` (۲۰ تست آفلاین) + `python trader.py quality --dataset …` روی داده‌ی واقعی |
| استراتژی + Risk Gate قطعی | سبز | **۱۲۵ تست آفلاین سبز**؛ چرخه‌ی paper امروز: ۴ تصمیم، صفر تأیید، صفر سفارش (علت `regime_filter`) |
| بک‌تست رویدادمحور | سبز | `POST /api/v1/runs/backtest` روی ۳۶۵ روز واقعی BTC → `run-6cbbd7a9b577`: ۲۳ معامله، خالص `−1.848%`، PF `0.203`، کارمزد `2.2861`، DD `1.85%` |
| موتور تحقیق (ضد overfit) | سبز | `python trader.py research --dataset … --grid "ema_fast=15,20;atr_stop_mult=1.5,2.5"` → split 1314/438/438، **REJECTED** چون candidate خارج‌نمونه صفر معامله داشت؛ champion حفظ شد و دلیل در Store ثبت شد |
| walk-forward از API با dataset واردشده | سبز (این مرحله) | `POST /runs/walk-forward` با `dataset_id` + ۴۰۰ روز واقعی → ۲۰۰ OK؛ داوری صادقانه «rejected» (۰ از ۲ پنجره سودآور، PF 0.298)، ۱۲ معامله‌ی out-of-sample، `risk_limits_untouched: true`، قهرمان ماند |
| ردّ داده‌ی ناکافی | سبز (این مرحله) | ۳۶۰ کندل → 422 «هر بخش حداقل ۱۲۰ کندل»؛ کلید `version` در grid → 422 صریح، نه 500 |
| نسخه‌بندی استراتژی | سبز | `python trader.py versions` → نسخه‌ها با digest، parent و متن تصمیم؛ تاریخچه حذف نمی‌شود |
| Store (SQLite) | سبز | `state/platform.sqlite3`: datasets 2 / bars 4380 / runs 4 / decisions 4 / snapshots 2 / versions 2 |
| Mirror رویدادها | سبز | هم چرخه‌ی CLI و هم `POST /cycles/paper` از یک مسیر مشترک (`src/cat/mirror.py`) می‌نویسند؛ dedupe روی کلید طبیعی → بدون رکورد تکراری (۲+۲ تصمیم = ۴، نه ۸) |
| API محلی | سبز | سرور واقعی روی `127.0.0.1:8177` (با کد اصلاح‌شده بازراه‌اندازی شد)؛ health/config/portfolio/runs/datasets/bars/analysis/risk/logs/reports + import/fetch/backtest/walk-forward/cycle/kill-switch؛ ۲۹ تست API |
| داشبورد (Vite+React+TS) | سبز (build + رندر) | ۱۳ صفحه (۷۱۹۵ خط)، `npx tsc --noEmit` صفر خطا، `vite build` موفق (۴۰۰KB JS / ۱۲۳KB gzip)؛ **هر ۱۳ مسیر در Edge هدلس PASS** (RTL/fa، صفر خطای کنسول، صفر خطای شبکه؛ ۳ مسیر موبایل هم PASS) |
| CI | push شد | مخزن خصوصی `mrabolfazl13/crypto-agentic-trader-kit`، main = `8bed332` + fix SPA؛ workflow: pytest + دروازه‌ی LIVE + audit + tsc + build؛ وضعیت اجرا از این ماشین قابل‌مشاهده نیست (بدون gh و بدون ابزار Actions) |

## مسیرهای رفع‌شده در این مرحله
- `UnboundLocalError` در `run_backtest` (تنظیم `params_label` پیش از ساخت `result`) — با تست موفقِ `/runs/backtest` پوشش داده شد؛ قبلاً فقط مسیرهای reject تست شده بودند.
- سری تأیید در API به‌جای کندل‌های 4h واقعی، همان کندل‌های سیگنال را alias می‌کرد → حالا `resample` به `confirm_timeframe` (۱d) انجام می‌شود و تست spacing 86400 دارد.
- `GET /datasets/{id}/bars` (با `overlays`) اضافه شد؛ EMA روی کل سری محاسبه و سپس بریده می‌شود تا مقدار seedشده از پنجره‌ی visible نباشد.
- `allocated_capital` به `/portfolio` اضافه شد (کارت «بازده خالص» داشبورد بدون آن همیشه خالی می‌ماند).
- `POST /runs/walk-forward` اکنون `dataset_id` و `days` را می‌پذیرد و `dataset_id` واقعی را در runs ثبت می‌کند (قبلاً همیشه NULL می‌نوشت).
- deep-linkهای SPA از API 404 می‌گرفتند (فقط در مرورگر پیدا شد، نه در tsc/build) → `SpaStaticFiles` با fallback به shell؛ assetهای گمشده همچنان 404.
- `params_from_dict` دیگر روی کلیدهای هویتی (`version/name/note`) TypeError نمی‌دهد و float غیرمتناهی را رد می‌کند؛ لایه‌ی research کلید `version` در grid را صریحاً 422 می‌کند.
- زیردستورهای CLI: `serve`، `fetch-history`، `import-csv`، `datasets`، `quality`، `runs`، `versions`، `research`.

## باقی‌مانده
- تست یکپارچگی end-to-end با زنجیره‌ی کامل API.
- paper چندروزه برای دروازه‌ی B.

## مرزهای ایمنی که دست‌نخورده می‌مانند
- پیش‌فرض و حالت فعلی: `PAPER`. `live_trading_enabled: false`. هیچ adapter زنده‌ای وجود ندارد و هیچ route یا دکمه‌ای LIVE را فعال نمی‌کند (تست: «no path contains live»).
- هیچ endpoint یا دکمه‌ای برای افزایش بودجه، تغییر سقف ریسک یا افزودن نماد ساخته نمی‌شود؛ صفحه‌ی Risk فقط‌خواندنی است.
- برداشت/انتقال/مارجین/اهرم: پیاده‌سازی نشده و ممنوع.
- Kill switch: `python trader.py stop` (و `POST /api/v1/kill-switch`) همیشه در دسترس؛ فقط سفارش‌های جدید را می‌بندد و موقعیت باز را به‌زور نمی‌فروشد.
- اعداد ساختگی در هیچ لایه‌ای نمایش داده نمی‌شود؛ نبود داده = `null` و پیام «داده‌ای موجود نیست».

## محدودیت‌های محیط (واقعی، نه حدسی)
- داکر/Postgres/Redis روی این ماشین نصب نیست → SQLite و سرویس محلی.
- داده از طریق پروکسی محلی `127.0.0.1:1080` گرفته می‌شود؛ بدون آن، همه‌چیز fail-closed است.
- ساعت این ماشین هم‌گام نیست؛ `clock_check` از زمان صرافی استفاده می‌کند و mtime فایل‌ها قابل اعتماد نیست.
- مرورگر Chrome نصب نیست؛ راستی‌آزمایی UI با Edge هدلس + CDP (پورت 9344) انجام می‌شود.
- اسلیپیج تحقق‌یافته در ledger ثبت نمی‌شود → در UI «ثبت نشده» نمایش داده می‌شود، نه عدد تخمینی.

## نتیجه‌ی دروازه‌ها
- **دروازه‌ی A (داده و بک‌تست): PASS از نظر مهندسی، FAIL از نظر edge.** ۸ پنجره‌ی BTC/ETH بین `−1.85%` و `+0.00%` خالص، profit factor اکثر پنجره‌ها زیر ۱، و استراتژی فقط در سال‌های نزولی از buy-and-hold جلو می‌افتد. **edge تأیید نشده؛ LIVE توجیه ندارد.**
- دروازه‌ی B (paper): در حال جمع‌آوری — چرخه‌ها ثبت می‌شوند و پس از restart بازیابی می‌شوند؛ برای «چند روز متوالی» هنوز شواهد کامل نیست.
- دروازه‌ی C (امنیت): تست‌های secrets/redaction و زنجیره‌ی ممیزی سبز.
- دروازه‌ی D (live): عمداً غیرقابل‌دسترس.
