# بورس تهران (TSE) — داده، پیکربندی و محدودیت‌ها

وضعیت این سند: **2026-10-24** — پیاده‌سازی خواندنی کامل شده است. مجموعه‌ی تست در این تاریخ `189 passed` است (۱۵۶ تست قبلی بدون شکست + ۳۳ تست بورس). صفحه‌ی `/tse` در Edge هدلس (دسکتاپ ۱۴۴۰×۹۰۰ و موبایل ۳۹۰×۸۴۴) با داده‌ی زنده رندر شد؛ دیدگاه بورس از سرویس کریپتو هم با `config.tse.yaml` همان‌جا راستی‌آزمایی شد.

> این فاز فقط **خواندن، تحلیل و Paper** است. برای بورس هیچ مسیر سفارش، هیچ اتصال کارگزاری و هیچ اعتبارنامه‌ای وجود ندارد. `mode` همیشه `PAPER` می‌ماند.

---

## ۱) منبع داده

منبع کارکرد، CDN عمومی TSETMC است:

```
https://cdn.tsetmc.com/api/...
```

نکته‌ی مهم: این CDN فقط با User-Agent مرورگری JSON می‌دهد. با UA پیش‌فرض پایتون، پوسته‌ی SPA برمی‌گردد. `TseProvider.USER_AGENT` و `HttpClient.headers` برای همین وجود دارند.

مسیرهای قدیمی `www.tsetmc.com/tsev2/data/*.aspx` و `api.tsetmc.com` در آزمون این ماشین کار نکردند (پوسته‌ی HTML / بنر سرویس). به آن‌ها وابستگی وجود ندارد.

### قرارداد endpointها

| endpoint | پاسخ | استفاده |
|---|---|---|
| `GET /api/ClosingPrice/GetMarketWatch?market=0&paperTypes[0]=1&withBestLimits=false&hEven=0&RefID=0` | `{"marketwatch":[…]}`, ۱۰۱۷ ردیف در snapshot ثبت‌شده |تابلوی کل بازار: قیمت آخر (`pcl`/`pdv`)، دیروز (`py`)، خرید/فروش (`pmd`/`pmo`)، بازه (`pMin`/`pMax`)، حجم (`qtj`)، ارزش (`qtc`)، تعداد معامله (`ztt`)، حجم مبنا (`bv`) |
| `GET /api/ClosingPrice/GetClosingPriceDailyList/{insCode}/{top}` | `{"closingPriceDaily":[…]}` | کندل روزانه: `priceFirst`→open، `priceMax`→high، `priceMin`→low، `pClosing`→close، `qTotTran5J`→volume |
| `GET /api/BestLimits/{insCode}` | `{"bestLimits":[…]}` | پنج سطح عمق سفارش |
| `GET /api/ClosingPrice/GetClosingPriceInfo/{insCode}` | `closingPriceInfo` + `instrumentState` | وضعیت ابزار |
| `GET /api/MarketData/GetMarketOverview/{flow}` | `marketOverview` | شاخص، وضعیت بازار، `marketActivityDEven` (روز واقعی جلسه) |
| `GET /api/Instrument/GetInstrumentSearch/{query}?instrumentType[0]=1&…` | `{"instrumentSearch":[…]}` | جست‌وجو؛ پاسخ ثبت‌شده برای پرس‌وجوی آزمایشی خالی بود و adapter چیزی از خود نمی‌سازد |

### تقویم و زمان

- جلسه: **شنبه تا چهارشنبه، ۰۹:۰۰–۱۲:۳۰ تهران**؛ پنجشنبه و جمعه تعطیل.
- تاریخ‌های `dEven` ثبت‌شده: `20261017,18,19,20,21` و سپس `20261014` — همین الگو مبنای تقویم است.
- `hEven` آخرین معامله `123002` است؛ پایان دادوستد پیوسته ۱۲:۳۰.
- `marketActivityHEven` زمان تولید گزارش است، نه زمان جلسه؛ در کد استفاده نمی‌شود.

**تاریخ نقل‌قول:** در `GetMarketWatch` تمام ردیف‌ها `dEven=0` دارند. بنابراین timestamp نقل‌قول از `marketActivityDEven` در `GetMarketOverview` گرفته می‌شود. اگر این مقدار در دسترس نباشد، `quote()` با `DataUnavailable` شکست می‌خورد؛ هرگز از ساعت دیوار («الان») استفاده نمی‌شود. نمونه‌ی راستی‌آزمایی‌شده: `quote.ts = 2026-10-21 09:00:02 UTC` (۱۲:۳۰:۰۲ تهران).

---

## ۲) جهان معاملاتی (`universe_tse.json`)

### چرا فیلتر ISIN؟

پارامترهای `paperTypes` در پاسخ زنده کل بورد را محدود نمی‌کنند (در آزمون، همه‌ی گونه‌ها ۳۴۶۲ ردیف برگرداندند) و snapshot ۱۰۱۷ ردیفی شامل ۴۱۶ حق تقدم/صندوق/اوراق است. فیلتر قابل اتکا، تاکسونومی خود بورس است:

- ISIN با پیشوند `IRO1`، `IRO3`، `IRO5` یا `IRO7`
- و سریال `0001` یا `0011` (سهام عادی)

حق تقدم‌ها (مثل `IRO1CRBN0003`)، صندوق‌ها (`IRT1…`) و اوراق (`IRB3…`) حذف می‌شوند.

### ساخت

```bash
python tools/build_tse_universe.py
python tools/build_tse_universe.py --limit 300 --min-value 10000000000
python tools/build_tse_universe.py --fixture tests/fixtures/tse_marketwatch_m1.json --no-write
```

خروجی `universe_tse.json` شامل `generated_at`، endpoint و پارامترها، `recorded_session_date`، `board_rows`، `count`، `digest`، `excluded` و فهرست رتبه‌بندی‌شده است. رتبه‌بندی بر اساس **ارزش معاملات (`qtc`) نزولی** و سپس تیکر است.

آخرین اجرای زنده (2026-10-23T22:29:50Z):

```
board_rows = 1017
kept       = 500
excluded   = {"beyond_limit": 101, "not_ordinary_share": 416}
digest     = 4600d16ab758df2021cf1156b152cbda8783b43ac65c4de08ce609e073d2779d
```

پنج نماد نخست: فولاد، ومعادن، وبملت، تاصيكو، خبهمن. نمونه‌ی فولاد: `insCode=46348559193224090`، ISIN `IRO1FOLD0001`، آخرین قیمت `4220`، دیروز `4340`، بازه‌ی مجاز `4210–4470`، ارزش معاملات `20,519,267,355,350` ریال.

`InstrumentIndex` تیکر، نام، ISIN و `insCode` عددی را به هم نگاشت می‌کند. تیکر فارسی فقط alias است؛ شناسه‌ی canonical همان `insCode` است.

---

## ۳) پیکربندی

`config.tse.example.yaml` یک فایل مستقل است؛ `config.yaml` کریپتو بازنویسی نمی‌شود. برای استفاده:

```bash
cp config.tse.example.yaml config.yaml   # پس از پشتیبان‌گیری از config فعلی
python tools/build_tse_universe.py       # اگر universe_tse.json ندارید
python trader.py check
python trader.py serve
```

مقادیر مهم و دلیل هرکدام:

| کلید | مقدار | دلیل |
|---|---|---|
| `trading.market` | `tse` | انتخاب registry؛ پیش‌فرض پروژه همچنان `crypto` است |
| `trading.mode` | `PAPER` | برای `tse` حتی با `LIVE` هم به `PAPER` برمی‌گردد |
| `trading.timeframe` | `1d` | TSETMC فقط تاریخچه‌ی روزانه می‌دهد |
| `data.confirm_timeframe` | `1w` | کندل هفتگی از تجمیع روزانه ساخته می‌شود (شنبه‌محور) |
| `markets.tse.max_symbols` | `40` | سقف اسکن از فهرست رتبه‌بندی‌شده |
| `markets.tse.fees` | `0.325%` | **پیش‌فرض تأییدنشده**؛ نرخ حساب خود را با کارگزار بررسی کنید |
| `risk.max_spread_pct` | `3.0` | spread تابلوی بورس معمولاً ۰.۲–۱٪ است؛ آستانه‌ی ۰.۳٪ کریپتو بیشتر نمادها را رد می‌کند |
| `risk.stop_on_data_staleness_seconds` | `345600` (۴ روز) | کندل روزانه + تعطیلی پنجشنبه/جمعه |
| `risk.allocated_capital` | `1,000,000,000` ریال | فقط حساب کاغذی |
| `risk.allow_short/margin/leverage/withdrawals` | `false` | config بورس این‌ها را اجباری خاموش می‌کند |

`markets.tse.symbols` اختیاری است؛ اگر نباشد، همه‌ی نمادهای universe (تا سقف `max_symbols`) اسکن می‌شوند. اگر مقدار بدهید، هر تیکر/`insCode` باید در `universe_tse.json` باشد؛ در غیر این صورت `load_config` با خطای صریح رد می‌شود. نبودن universe هم خطای صریح است، نه جهان خالی.

---

## ۴) API خواندنی

همه‌ی مسیرها زیر `/api/v1` و فقط `GET` هستند. دو حالت پشتیبانی می‌شود:

1. سرویس با `trading.market: tse` — همان config هم موتور و هم تابلو را تغذیه می‌کند.
2. سرویس کریپتو (پیش‌فرض نصب) + فایل `config.tse.yaml` در همان ریشه — این فایل **فقط** دیدگاه فقط‌خواندنیِ بورس را تغذیه می‌کند؛ engine، ریسک و چرخه‌ی paper روی بازار اصلی می‌مانند (`/health` هنوز `crypto` گزارش می‌کند). نبود فایل یا `trading.market` اشتباه داخل آن → `409` با دستور ساخت (کپی `config.tse.example.yaml`).

| مسیر | محتوا |
|---|---|
| `GET /tse/market?flow=1` | وضعیت جلسه (تقویم تهران)، شاخص/ارزش/تعداد معاملات، اندازه‌ی تابلوی پالایش‌شده، allowlist، `freshness_reference`، `provenance.single_source=true` |
| `GET /tse/instruments?limit=50` | نمادهای مجاز با قیمت آخرین جلسه، تغییر، خرید/فروش، حجم، ارزش، بازه؛ `universe` شامل digest/source، `provenance` |
| `GET /tse/instrument?symbol=…` | `display` (insCode/ticker/name/ISIN)، `band` (کف/سقف مجاز و مبنای آن)، `quote` (bid/ask/mid/spread/ts/source) یا `null` + `quote_note`، `venue_rules` (lot=100، ریال، سهم) |
| `GET /tse/book?symbol=…` | پنج سطح نخست کتاب سفارش + `note` وقتی عمق منتشر نشده یا یک سمت صف است |

خطاها: `404` برای نماد خارج از universe/allowlist، `409` برای نبودِ config دیدگاه بورس، `503` فقط وقتی TSETMC خوانده نمی‌شود. نبودِ قیمت دوطرفه (نماد متوقف یا یک‌طرفه) **شکست نیست**: `200` با `quote: null` و `quote_note`، و در کتاب سفارش سطح بدون سفارش `price: null` می‌گیرد — هرگز صفر. هیچ `POST`ی برای بورس وجود ندارد؛ `POST /tse/market` → `405`.

نمادهای TSE در `/market/candles` هم کار می‌کنند: `GET /market/candles?symbol=46348559193224090&timeframe=1d&limit=180` با همان `provenance` (منبع `tsetmc:…`). تایم‌فریم مجاز فقط `1d` و `1w` است.

---

## ۵) تازگی داده

مشکل واقعی: منطق ۲۴/۷ کریپتو، هر اسنپ‌شات بورس را در شب و آخر هفته `stale_klines`/`stale_quote` می‌کرد و عملاً سیستم را خاموش می‌ساخت.

راه‌حل، بدون تغییر رفتار کریپتو:

- `MarketSpec.gap_is_expected` برای بورس به `calendar.bar_gap_ok` وصل است: شکاف چهارشنبه→شنبه و شنبه→شنبه پذیرفته می‌شود، اما جلسه‌ی واقعاً گم‌شده هنوز `time_gap` است.
- `fetch_snapshot`/`validate_klines` پارامتر اختیاری `gap_ok` و `staleness_reference_ts` دارند. پیش‌فرض `None` است؛ کریپتو دقیقاً مثل قبل با ساعت دیوار سنجیده می‌شود.
- `calendar.staleness_reference()` ساعت مرجع را در پایان آخرین جلسه نگه می‌دارد (مثلاً جمعه شب → چهارشنبه ۱۲:۳۰ تهران).
- `engine.snapshot()` این مرجع و `gap_ok` را از spec می‌گیرد.

اثبات آفلاین: `tests/test_tse_freshness.py` (۴ تست) نشان می‌دهد snapshot جمعه‌شب تازه است، هشدار `single_source_tsetmc` دارد، و همان feed قدیمی کریپتو همچنان stale می‌شود.

---

## ۶) صفحه‌ی `/tse`

- `web/src/api/tseClient.ts`: تایپ‌های پاسخ‌ها و چهار تابع GET؛ هیچ mutation ندارد.
- `web/src/pages/tse/TseMarket.tsx`: وضعیت جلسه/شاخص، جستجو و انتخاب نماد، قیمت/بازه‌ی مجاز/اسپرد، نمودار روزانه با خطوط کف و سقف، کتاب سفارش پنج‌سطحی و جدول قابل مرتب‌سازی نمادها.
- ناوبری: گروه «بورس ایران» → «بورس تهران»؛ route در `App.tsx`.
- زمان‌های جلسه و نقل‌قول به وقت تهران نمایش داده می‌شوند؛ محور نمودار (کامپوننت مشترک) UTC است و این نکته زیر نمودار نوشته شده است.

اعتبارسنجی انجام‌شده: `npx tsc --noEmit` بدون خطا، `npm run build` موفق، و رندر واقعی در Edge هدلس روی دسکتاپ ۱۴۴۰×۹۰۰ و موبایل ۳۹۰×۸۴۴؛ هر چهار مسیر `/tse/*` و `/market/candles` پاسخ `200` دادند، نمودار/بازه/کتاب سفارش نمایش داده شد، `scrollWidth == innerWidth` و صفر خطای کنسول.

---

## ۷) مرزهای ایمنی

- `trading.market` پیش‌فرض `crypto` است؛ نصب تازه و `config.yaml` فعلی دست‌نخورده‌اند.
- برای `tse` هیچ کلاس/تابع سفارش وجود ندارد: `TseProvider` فقط `watch/quote/book/klines/filters/price_band/market_state/search` دارد.
- هیچ route بورس `POST` نیست؛ هیچ endpoint‌ای `mode` را تغییر نمی‌دهد.
- `mode=LIVE` برای بورس به `PAPER` برمی‌گردد و هشدار ثبت می‌شود؛ `allow_short/margin/leverage` اجباری `false` است.
- کارمزد و lot اعداد پیش‌فرض مستندشده‌اند، نه ادعای تأیید کارگزار. هیچ قیمت/کارمزد/گام ساختگی در پاسخ‌ها نیست؛ نبود داده `null` + پیام است.

---

## ۸) محدودیت‌های باقی‌مانده

1. **بدون منبع دوم مستقل**: cross-check بورس خاموش است و هر snapshot هشدار `single_source_tsetmc` دارد.
2. **بدون API رسمی سفارش کارگزار**: فقط تحلیل/بک‌تست/Paper. هیچ برنامه‌ای برای ساخت مسیر سفارش زنده در این مخزن وجود ندارد.
3. **فقط سهام عادی**: حق تقدم، صندوق و اوراق عمداً از universe حذف شده‌اند.
4. **فقط تاریخچه‌ی روزانه**: تایم‌فریم‌های زیرروزانه از TSETMC منتشر نمی‌شوند. کندل هفتگی از تجمیع روزانه ساخته می‌شود و کندل هفته‌ی جاری `closed=false` است.
5. **سقف `top=1000`**: تاریخچه‌ی روزانه تا حدود ۲۰۲۲ برمی‌گردد؛ قدیمی‌تر نیازمند pagination است که پیاده نشده.
6. **کارمزد تأییدنشده**: `0.325%` فقط پیش‌فرض قابل تغییر است.
7. **بک‌تست بورس هنوز کامل به spec وصل نیست**: `backtest.py`/`month_scan.py` مسیر `GateProvider` را مستقیم صدا می‌زنند؛ این یک مرحله‌ی جدا و شناخته‌شده است.

---

## ۹) شواهد اجراشده

| تاریخ | شاهد |
|---|---|
| 2026-10-23 | `tools/build_tse_universe.py` زنده: ۱۰۱۷ ردیف تابلو، ۵۰۰ سهم عادی، digest `4600d16a…` |
| 2026-10-21/24 | `quote()` زنده‌ی فولاد: `ts=2026-10-21T09:00:02Z` (۱۲:۳۰:۰۲ تهران)، bid/ask `4230/4240` |
| 2026-10-24 | `python -m pytest -q` → **181 passed** (۱۵۴ کریپتو + ۱۵ mapping/تقویم + ۴ freshness + ۸ safety) |
| 2026-10-24 | `npx tsc --noEmit` صفر خطا؛ `npm run build` موفق |

فایل‌های شاهد آفلاین: `tests/fixtures/tse_marketwatch_m1.json`، `tse_daily_foolad.json`، `tse_bestlimits_foolad.json`، `tse_marketoverview_f1.json`، `tse_search_foolad.json`.
