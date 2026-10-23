# CURRENT_STATE

به‌روزرسانی: 2026-10-23T21:25Z (UTC) — جهان معاملاتی ۱۰۰ نمادی، نمودار کندل زنده برای همه‌ی نمادها، فهرست تصمیم‌ها با اسکرول داخلی و سقف `/decisions`؛ **۱۴۲ تست سبز**.

## لایه‌هایی که الان کار می‌کنند (راستی‌آزمایی‌شده با اجرا)
| لایه | وضعیت | شواهد (همه اجراشده، نه ادعا) |
|---|---|---|
| داده‌ی بازار واقعی | سبز | `python trader.py check` → `check: PASS`؛ `fetch-history` → `ds-ex-43cf0e785f` (BTC 4h، ۲۱۹۰ کندل بسته، coverage 100%) و `ds-ex-0f36cb81e7` (ETH 4h)؛ gateio/mexc از طریق پروکسی 200 OK |
| جهان معاملاتی ۱۰۰ نمادی | سبز (این مرحله) | `python tools/build_universe.py` → ۱۰۰ جفت USDT از Gate به ترتیب ارزش بازار CoinGecko؛ `universe.json` با snapshot `2026-10-23T20:39:25+00:00`، `digest 65f5f4cb…`، فهرست حذف‌شده‌ها (استیبل‌کوین/طلا/رپ‌شده) و ۲۰۳۲ جفت tradable بررسی‌شده؛ `config.yaml` به‌صورت جراحی بازنویسی شد |
| نمایش جهان در UI/API | سبز | `GET /api/v1/universe` منبع و snapshot را برمی‌گرداند؛ کارت «جهان معاملاتی» در داشبورد تعداد/منبع/digest را نشان می‌دهد؛ `GET /config` شمارش را تأیید می‌کند |
| موتور روی ۱۰۰ نماد | سبز | چرخه‌ی paper واقعی: **۱۰۰ outcome، ۰ خطا**، ۵m39s؛ یک BUY: BNB_USDT ۰٫۰۶۷ @ 742.6، کارمزد 0.049754، stop 731.959819، target 767.427089، equity ≈ 999.95؛ چرخه‌ی دوم ۴m44s → HOLD، بدون سفارش تکراری، ۰ خطا؛ `max_concurrency: 4` |
| اعتبارسنجی داده | سبز | `tests/test_datasets.py` (۲۰ تست آفلاین) + `python trader.py quality --dataset …` روی داده‌ی واقعی |
| استراتژی + Risk Gate قطعی | سبز | **۱۴۲ تست آفلاین سبز** (`pytest -q` → `142 passed`)؛ شامل رگرسیون تاریخچه‌ی کوتاه (EMA خالی) |
| بک‌تست رویدادمحور | سبز | `POST /api/v1/runs/backtest` روی ۳۶۵ روز واقعی BTC → `run-6cbbd7a9b577`: ۲۳ معامله، خالص `−1.848%`، PF `0.203`، DD `1.85%` |
| موتور تحقیق (ضد overfit) | سبز | `python trader.py research … --grid "ema_fast=15,20;atr_stop_mult=1.5,2.5"` → split 1314/438/438، **REJECTED**؛ champion حفظ شد |
| walk-forward از API | سبز | `POST /runs/walk-forward` با `dataset_id` + ۴۰۰ روز واقعی → ۲۰۰ OK؛ «rejected» (۰ از ۲ پنجره سودآور، PF 0.298)، ۱۲ معامله‌ی OOS، `risk_limits_untouched: true` |
| ردّ داده‌ی ناکافی | سبز | ۳۶۰ کندل → 422 «هر بخش حداقل ۱۲۰ کندل»؛ کلید `version` در grid → 422 صریح، نه 500 |
| نسخه‌بندی استراتژی | سبز | `python trader.py versions` → نسخه‌ها با digest، parent و متن تصمیم |
| Store (SQLite) | سبز | `state/platform.sqlite3`: datasets 3 / bars 6780 / runs 7 / decisions 226 / orders 1 / fills 1 / portfolio_snapshots 16 / strategy_versions 2 |
| Mirror رویدادها | سبز | چرخه‌ی CLI و `POST /cycles/paper` از یک مسیر (`src/cat/mirror.py`) می‌نویسند؛ dedupe روی کلید طبیعی |
| کندل زنده‌ی بازار | سبز (این مرحله) | `GET /api/v1/market/candles?symbol=SOL_USDT&timeframe=4h&limit=180` → ۱۸۰ کندل، ۱۷۹ بسته، `source gate:SOL_USDT:4h`، window `2026-09-24 → 2026-10-23`؛ allowlist خارج از جهان → 404؛ timeframe نامعتبر → 400؛ صرافی در دسترس نیست → 503؛ کش ۲۰ ثانیه‌ای درون‌فرآیندی |
| نمودار همه‌ی نمادها | سبز (این مرحله) | کارت بازار به `getUniverse` وصل شد: **۱۰۰ گزینه** در انتخابگر (جستجو + select)، پیش‌فرض BTC_USDT، نمودار پس از انتخاب SOL با ۱۷۹ مستطیل کندل در Edge هدلس رندر شد؛ صفر خطای کنسول |
| فهرست تصمیم‌ها با اسکرول | سبز (این مرحله) | `/decisions?limit=50` → دقیقاً ۵۰ رکورد؛ کادر `.feed-scroll` با ارتفاع 46vh، اسکرول‌بار مرئی و `tabIndex=0`؛ اندازه‌گیری: viewport ۴۱۲px / content ۲۷۰۶px، چرخ ماوس ۰→۳۲۰ بدون جابه‌جایی کل صفحه؛ ارتفاع کل صفحه از ۷۳۷۶px به ۲۲۸۸px رسید |
| نشانی کش پوسته | سبز | `SpaStaticFiles`: shell → `no-store, must-revalidate`؛ asset هش‌دار → `immutable` (رفع پوسته‌ی کهنه پس از deploy) |
| API محلی | سبز | سرور واقعی روی `127.0.0.1:8177`؛ health/config/portfolio/runs/datasets/bars/analysis/risk/logs/reports/universe/market-candles + import/fetch/backtest/walk-forward/cycle/kill-switch |
| داشبورد (Vite+React+TS) | سبز (build + رندر) | ۱۳ صفحه؛ `npx tsc --noEmit` صفر خطا؛ `vite build` موفق؛ مسیرها در Edge هدلس PASS (RTL/fa، صفر خطای کنسول/شبکه، ۳ مسیر موبایل ۳۹۰×۸۴۴) |
| CI | push شد | مخزن خصوصی `mrabolfazl13/crypto-agentic-trader-kit`؛ workflow: pytest + دروازه‌ی LIVE + audit + tsc + build؛ وضعیت اجرا از این ماشین قابل‌مشاهده نیست (بدون gh/Actions tools) |

## مسیرهای رفع‌شده در این مرحله
- `TypeError` در `strategy.analyze()` روی نمادهای تازه‌لیست (EMA هنوز seed نشده) → سناریوها به‌جای فرمت‌کردن `None`، «unavailable: needs N closes» برمی‌گردانند؛ تست رگرسیون اضافه شد.
- خطای استراتژی در یک نماد کل چرخه را 500 می‌کرد → موتور هر خطای strategy را در audit ثبت می‌کند و با `consecutive_errors`/`safe_state` **fail-closed** می‌شود، بدون از دست دادن کل اسکن.
- «سقف موقعیت همزمان» در Settings خالی بود → `/config` فیلد `max_open_positions` را برمی‌گرداند.
- `/decisions` پارامتر `limit` را نادیده می‌گرفت (۱۰۲ ردیف / صفحه‌ی ۷۳۷۶px) → `limit` با بازه‌ی ۱..۱۰۰۰ اضافه شد؛ داشبورد ۵۰ می‌خواهد و داخل کادر اسکرول می‌کند.
- اسکرول نامرئی بود → کادر مرزدار با پس‌زمینه‌ی متمایز، اسکرول‌بار ضخیم‌تر و فوکوس‌پذیر + هدر `no-store` برای پوسته.
- نمودار فقط از datasetهای ذخیره‌شده می‌خواند (عملاً BTC) → endpoint زنده‌ی `/market/candles` روی allowlist جهان و اتصال کارت بازار به `getUniverse`؛ حالا هر ۱۰۰ نماد انتخاب‌شدنی‌اند.
- deep-linkهای SPA از API 404 می‌گرفتند → `SpaStaticFiles` با fallback به shell؛ assetهای گم‌شده همچنان 404.

## باقی‌مانده
- تست یکپارچگی end-to-end با زنجیره‌ی کامل API.
- paper چندروزه برای دروازه‌ی B.
- بررسی واکنش‌گرایی جدول‌های بک‌تست در موبایل (خارج از این مرحله).

## مرزهای ایمنی که دست‌نخورده می‌مانند
- پیش‌فرض و حالت فعلی: `PAPER`. `live_trading_enabled: false`. هیچ adapter زنده‌ای وجود ندارد و هیچ route یا دکمه‌ای LIVE را فعال نمی‌کند (تست: «no path contains live»).
- هیچ endpoint یا دکمه‌ای برای افزایش بودجه، تغییر سقف ریسک یا افزودن نماد خارج از جهان ثبت‌شده ساخته نشده؛ صفحه‌ی Risk فقط‌خواندنی است.
- برداشت/انتقال/مارجین/اهرم: پیاده‌سازی نشده و ممنوع؛ `allow_margin/allow_leverage/allow_withdrawals: false`.
- Kill switch: `python trader.py stop` و `POST /api/v1/kill-switch` همیشه در دسترس؛ فقط سفارش‌های جدید را می‌بندد.
- اعداد ساختگی در هیچ لایه‌ای نمایش داده نمی‌شود؛ نبود داده = `null` و پیام «داده‌ای موجود نیست».

## محدودیت‌های محیط (واقعی، نه حدسی)
- داکر/Postgres/Redis روی این ماشین نصب نیست → SQLite و سرویس محلی.
- داده از طریق پروکسی محلی `127.0.0.1:1080` گرفته می‌شود؛ بدون آن، همه‌چیز fail-closed است.
- ساعت این ماشین هم‌گام نیست؛ `clock_check` از زمان صرافی استفاده می‌کند و mtime فایل‌ها قابل اعتماد نیست.
- مرورگر Chrome نصب نیست؛ راستی‌آزمایی UI با Edge هدلس + CDP (پورت 9344) انجام می‌شود.
- اسلیپیج تحقق‌یافته در ledger ثبت نمی‌شود → در UI «ثبت نشده» نمایش داده می‌شود، نه عدد تخمینی.

## نتیجه‌ی دروازه‌ها
- **دروازه‌ی A (داده و بک‌تست): PASS از نظر مهندسی، FAIL از نظر edge.** پنجره‌های BTC/ETH بین `−1.85%` و `+0.00%` خالص، PF اکثر پنجره‌ها زیر ۱؛ **edge تأیید نشده؛ LIVE توجیه ندارد.** گسترش به ۱۰۰ نماد فقط پوشش بازار را بیشتر می‌کند، نه وجود edge.
- دروازه‌ی B (paper): در حال جمع‌آوری — چرخه‌ها ثبت و پس از restart بازیابی می‌شوند؛ برای «چند روز متوالی» هنوز شواهد کامل نیست.
- دروازه‌ی C (امنیت): تست‌های secrets/redaction و زنجیره‌ی ممیزی سبز.
- دروازه‌ی D (live): عمداً غیرقابل‌دسترس.
