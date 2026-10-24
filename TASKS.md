# TASKS

معیار پذیرش هر آیتم: کد قابل‌اجرا + تست سبز + شواهد اندازه‌گیری‌شده. بدون «طرح» به‌عنوان خروجی نهایی.
وضعیت تا 2026-10-24 — مجموعه‌ی تست: **۲۰۹ passed** (`python -m pytest -q`)؛ آموزش/اعتبارسنجی پروفایل‌ها، افق زمانی، API و رندر مرورگری هم پوشش داده شده‌اند.

---

# فاز بورس ایران (TSE) — ماژولار، بدون آسیب به مسیر کریپتو

این بخش **تنها مرجع** کار بورس است: قرارداد کشف‌شده، تصمیم‌ها، وضعیت فایل‌به‌فایل و کارهای باقی‌مانده. به‌روز می‌شود تا فاز بسته شود.
معیار سخت: هیچ تست کریپتویی (۱۵۴) نباید بشکند و هیچ رفتار مسیر `market: crypto` نباید تغییر کند.

## ۰) قرارداد داده‌ی TSETMC — کشف و تأیید با اجرای واقعی (نه حدس)
ماشین به اینترنت داخلی دسترسی مستقیم دارد (بدون پروکسی). نتایج آزمون‌های انجام‌شده:
- `www.tsetmc.com/tsev2/data/*.aspx` و `Loader.aspx` **مرده‌اند**: SPA shell (۸۲۴ بایت، `text/html`) برمی‌گردانند.
- `api.tsetmc.com` فقط بنر `Api Serivce For tsetmc.com v:151` می‌دهد؛ همه‌ی route حدسی 404.
- **منبع کارکرد: `https://cdn.tsetmc.com/api`** — با User-Agent مرورگر JSON می‌دهد؛ با UA پیش‌فرضِ python همان shell را می‌دهد (به همین دلیل `HttpClient.headers` و `TseProvider.USER_AGENT` لازم است).

| endpoint | پاسخ واقعی (ثبت‌شده) | استفاده |
|---|---|---|
| `GET /api/ClosingPrice/GetMarketWatch?market=0&paperTypes[0]=1&withBestLimits=false&hEven=0&RefID=0` | `{"marketwatch":[…]}` — **۱۰۱۷ ابزار**؛ کلیدها: `insCode insID lva lvc py pf pcl pdv pmd pmo pmn pmx pMin pMax qtj qtc ztt bv pe csv ztd blDs` | universe، bid/ask (`pmd/pmo`)، دامنه‌ی مجاز روز (`pMin/pMax`)، حجم مبنا (`bv`) |
| `GET /api/ClosingPrice/GetClosingPriceDailyList/{insCode}/{top}` | `{"closingPriceDaily":[…]}` — `top=1000` واقعی تا `20220820`؛ `priceFirst priceMax priceMin pClosing pDrCotVal qTotTran5J qTotCap zTotTran dEven hEven yClose iClose priceYesterday priceChange` | کندل روزانه: open=`priceFirst`، high=`priceMax`، low=`priceMin`، close=`pClosing` |
| `GET /api/BestLimits/{insCode}` | `{"bestLimits":[…]}` — `pMeDem/pMeOf + qTitMeDem/qTitMeOf + zOrdMe*` | عمق سفارش برای داشبورد |
| `GET /api/ClosingPrice/GetClosingPriceInfo/{insCode}` | `closingPriceInfo` + `instrumentState.cEtavalTitle: "مجاز"` | وضعیت ابزار/آخرین به‌روزرسانی |
| `GET /api/MarketData/GetMarketOverview/{flow}` | `indexLastValue 67443.70 / 817367376…`, `marketStateTitle: "بسته"`, `marketActivityDEven 20261021` | وضعیت بازار و شاخص (flow 1=بورس، 2=فرابورس) |
| `GET /api/Instrument/GetInstrumentSearch/{query}?instrumentType[0]=1&…` | `{"instrumentSearch":[…]}` با `insCode lVal18AFC lVal30 flow cgrValCot` | جست‌وجوی نماد (فارسی URL-encode) |

شواهد خام در `tests/fixtures/tse_*.json` ذخیره شد (marketwatch بورس، daily/٣۰ روزه‌ی فولاد مبارکه، bestLimits، marketOverview).

**استخراج تقویم از خود داده:** تاریخ‌های `dEven` پشت‌سرهم `20261017,18,19,20,21` و بعد `20261014` — یعنی شنبه تا چهارشنبه جلسه دارد و پنجشنبه/جمعه نه. `hEven` آخرین معاملات `122959`/`123002` → پایان دادوستد پیوسته ۱۲:۳۰. (`marketActivityHEven=180933` زمان گزارش است، نه جلسه — در کد استفاده نشده.)

## ۱) تصمیم‌های معماری (چرا این‌شکل، تا کریپتو لطمه نبیند)
- [x] **مرکز ثقل = registry، نه fork**: `src/cat/markets/` با `MarketSpec` (provider/cross/clock/freshness/allowlist). مسیر فعلی کریپتو فقط یک spec نازک است که به `GateProvider`/`MexcCrossCheck`/همان URL ساعت delegate می‌کند — بدون بازنویسی منطق.
- [x] **`canonical_symbol` اختیاری (duck-typed)**: `fetch_snapshot` به‌جای `normalize_pair(symbol)` از `getattr(gate,"canonical_symbol",normalize_pair)` استفاده می‌کند. کریپتو این متد را ندارد → دقیقاً رفتار قبلی؛ بورس کد عددی `insCode` را canonical می‌کند (تیکر ۴ حرفی فارسی فقط alias).
- [x] **تازگی داده جلسه‌محور**: مشکل واقعی — با منطق ۲۴/۷، هر اسنپ‌شات بورس در شب `stale_klines`/`stale_quote` می‌خورد و کل سیستم عملاً خاموش می‌شد. راه‌حل: `staleness_reference_ts` اختیاری در `validate_klines`/`fetch_snapshot` (پیش‌فرض `None` = ساعت واقعی = تغییرناپذیر برای کریپتو) و `calendar.staleness_reference()` که ساعت تازگی را در پایان آخرین جلسه نگه می‌دارد.
- [x] **کارمزد/tick ساختگی نیست**: گام قیمت از جدول حدسی نمی‌آید؛ از `pMin/pMax` خود صرافی و `price_precision=0` (ریال صحیح) استفاده می‌شود. `LOT_SIZE=100` به‌عنوان قانون تالار با کامنت «با کارگزار تأیید شود». `min_quote/max_quote = 0` (چنین سقفی از TSETMC منتشر نشده).
- [x] **cross-check صادقانه**: بورس منبع دوم مستقل ندارد → `cross=None` و هشدار `single_source_tsetmc` روی هر snapshot (نه یک منبع جعلی).
- [x] **clock**: TSETMC endpoint زمان ندارد → `HttpClient.date_header_epoch()` از هدر `Date` خود CDN (رکور واقعی، دقت ۱ ثانیه).
- [x] **کارایی**: یک `GetMarketWatch` کل بورد را با TTL ۲۰s کش می‌کند → سیکل ۱۰۰ نمادی بورس چند درخواست است نه چندصد؛ `klines` هم per-symbol کش دارد.
- [x] **UI**: مسیر و صفحه‌ی جدا (`/tse`) با client جدا (`web/src/api/tseClient.ts`)؛ صفحات فعلی دست‌نخورده.
- [ ] **live**: برای بورس هم هیچ مسیر اجرا زنده‌ای ساخته نمی‌شود (بدون API رسمی کارگزار؛ فقط تحلیل/بک‌تست/Paper).

## ۲) وضعیت فایل‌ها
### بک‌اند
- [x] `src/cat/markets/calendar.py` — تقویم Tehran (شنبه–چهارشنبه، ۰۹:۰۰–۱۲:۳۰)، `last_session_close`، `next_session_open`، `phase()`، `staleness_reference()`
- [x] `src/cat/markets/base.py` — `MarketSpec` + registry (`register/get_spec/names/describe`)
- [x] `src/cat/markets/crypto.py` — spec کریپتو روی همان کلاس‌های قبلی (رفتار قبلی، بدون تغییر)
- [x] `src/cat/markets/tse.py` — `TseProvider` (watch/quote/book/closing_info/klines/klines_range/filters/price_band/market_state/server_time/search) + `InstrumentIndex` از `universe_tse.json`
- [x] `src/cat/markets/__init__.py` — export registry
- [x] `src/cat/http.py` — `headers` روی `HttpClient`، `date_header_epoch()`
- [x] `src/cat/data.py` — hook های `canonical_symbol`، `staleness_reference_ts` و `gap_ok`
- [x] `src/cat/config.py` — `trading.market` (پیش‌فرض `crypto`) + بخش `markets:` + allowlist/fee از بورس + warning برای تایم‌فریم نامعتبر
- [x] `src/cat/engine.py` — provider/cross/clock از spec + `spec.freshness_reference` + `spec.gap_is_expected`
- [x] `src/cat/api.py` — route های فقط‌خواندنی `/api/v1/tse/*` با `provenance`
- [x] `tools/build_tse_universe.py` + `universe_tse.json` — ۵۰۰ سهم عادی، snapshot/digest/منبع، حذف ۴۱۶ ردیف غیرعادی
- [x] `config.tse.example.yaml` — پیکربندی Paper بورس (timeframe 1d، `max_spread_pct 3.0`، تازگی ۴ روزه، بودجه‌ی ریالی)

### تست (۱۵۶ تست قبلی سبز ماند؛ کل مجموعه ۱۸۹)
- [x] `tests/test_markets_tse.py` — 17 تست آفلاین با fixtureهای ضبط‌شده: mapping فیلدها، canonical symbol، book، band، timeframe نامعتبر → `DataUnavailable`، single-source، تقویم، لنگر شنبه، نقل‌قول جلسه‌محور
- [x] `tests/test_tse_freshness.py` — ۴ تست: snapshot بورس در شب/آخر هفته stale **نیست**، کریپتو همچنان stale می‌شود، شکاف واقعی جلسه هنوز `time_gap` است
- [x] `tests/test_tse_safety.py` — 12 تست: با `market: tse` هیچ مسیر LIVE نیست، `LIVE` به PAPER برمی‌گردد، short/margin/leverage خاموش، allowlist/universe اجباری، API فقط GET، kill switch موجود
- [x] `pytest -q` کل مجموعه → **189 passed**

### UI
- [x] `web/src/api/tseClient.ts` — تایپ‌ها + fetch مسیرهای بورس
- [x] `web/src/pages/tse/TseMarket.tsx` — وضعیت بازار/شاخص، جدول universe با جستجو، کندل+عمق، band روز، برچسب منبع
- [x] `web/src/nav.ts` + `web/src/App.tsx` — آیتم «بورس ایران» و route (فقط افزودن)
- [x] `npx tsc --noEmit` صفر خطا + `vite build` موفق
- [x] رندر واقعی `/tse` در Edge هدلس (دسکتاپ ۱۴۴۰×۹۰۰ و موبایل ۳۹۰×۸۴۴) — صفر خطای کنسول، بدون overflow افقی

### مستندات
- [x] `docs/TSE_MARKET_FA.md` — قرارداد endpointها، محدودیت‌ها (بدون منبع دوم، بدون API رسمی سفارش، lot/tick)، نحوه‌ی تولید universe
- [x] به‌روزرسانی `docs/API_CONTRACT_FA.md` (بخش `/tse/*`) و `CURRENT_STATE.md`

## ۲-ب) ایرادهای دنیای‌واقعی که از داشبورد زنده گزارش شدند و رفع شدند (2026-10-24)
هر پنج مورد از صفحه‌ی در حال اجرا دیده شد، نه از تست؛ همه با شواهد مرورگری بسته شدند.

- [x] **`/tse/market` خروجی `text/html` می‌داد**: سرورِ روی ۸۱۷۷ قبل از ساخت route های بورس بالا آمده بود. فرانت حالا دو لایه تشخیص می‌دهد: اگر `content-type` JSON نبود یا ساختار پاسخ با تایپ اعلام‌شده نمی‌خواند، `ApiError` صریح («نسخه‌ی قدیمی سرور در حال اجراست») می‌شود — نه بلنک رندر (`web/src/api/tseClient.ts`: `getJSON` + `requireShape`).
- [x] **باگ ویندوزی SPA fallback**: `StaticFiles.get_path` مسیر را با `\` می‌دهد (`api\v1\…`)، پس `startswith("api/")` روی ویندوز هرگز درست نبود و ۴۰۴های API به shell تبدیل می‌شدند. تصمیم حالا از `scope["path"]` (URL) با `_is_client_route` گرفته می‌شود؛ `/api/v1/tse/does-not-exist` → ۴۰۴ JSON، `/tse` → shell (تست: `tests/test_api.py`).
- [x] **بن‌بست «سرویس جدا بگیرید» (409)**: به‌جای راه‌اندازی دوم، دیدگاه دوم اضافه شد — `config.tse.yaml` در همان ریشه، فقط‌خواندنی (`_venue_view_config` + `SECONDARY_VIEWS` + `_resolve_symbol_config`)؛ `/health` همچنان `crypto`/`PAPER` است و موتور/ریسک/paper دست‌نخورده. کش کندل و provider حالا با `market` نام‌فضایی‌شده‌اند (تست‌ها: `test_tse_board_is_readable_from_a_crypto_primary_service`، `test_tse_view_config_cannot_smuggle_the_primary_market`). فایل `config.tse.yaml` نمونه در ریشه‌ی مخزن اضافه شد.
- [x] **تغییر شاخص به‌صورت درصد چاپ می‌شد** (`−98,676.99%`): TSETMC مقدار `indexChange` را بر حسب **واحد شاخص** منتشر می‌کند؛ درصد از `index_last_value − index_change` مشتق می‌شود و با ایزولاتورهای دوجهته (U+2066/U+2069) داخل جمله‌ی RTL قرار می‌گیرد، وگرنه `−1.19%` به شکل `1.19%−` (شبیه رشد) خوانده می‌شود. راستی‌آزمایی با dump کدپوینت و برش اسکرین‌شات.
- [x] **«داده‌ی این ابزار خوانده نشد: no two-sided quote for … (خبهمن)» — ۵۰۳ اشتباه بود**: نداشتن قیمت دوطرفه «داده‌ی غایب» است نه قطعی سرویس. `/tse/instrument` حالا `200` با `quote: null` + `quote_note` برمی‌گرداند (band، venue_rules و کندل‌ها از همان منبع واقعی می‌مانند) و UI حالت «بدون نقل‌قول» را با متن فارسی + دلیل فنی `dir=ltr` توضیح می‌دهد. در `/tse/book` هم سطحِ بدون سفارش `price: null` است (نه `0`) و `note` می‌گوید «صف خرید/فروش قفل است»؛ قطع واقعی TSETMC همچنان `503` و fail-closed است.
  - تست‌ها: `test_one_sided_board_degrades_to_null_instead_of_failing_the_card`، `test_locked_queue_is_named_in_the_book_note` (safety) و `test_empty_side_of_the_book_is_an_absent_price_not_zero`، `test_one_sided_marketwatch_row_refuses_a_quote` (provider).
  - شواهد زنده روی `127.0.0.1:8177` (Edge CDP 9344، `~/.qoder-tmp/tse_both_states.mjs`): فولاد دوطرفه → خرید 4,230 / فروش 4,240 / اسپرد 0.24%؛ خبهمن → «داده‌ای موجود نیست» + یادداشت، ۱۸۷ کندل بسته از `tsetmc:26824673819862694:1d`، کتاب ۵ سطحه با ۰ سفارش فروش؛ بازگشت به فولاد → همان اعداد قبلی. صفر خطای کنسول، صفر پاسخ غیر ۲۰۰، صفر بلوک خطای مرئی.
  - `~/.qoder-tmp/tse-8177/quote-two-sided.png` · `quote-one-sided.png` · `crypto-after-tse-fixes.png` (داشبورد کریپتو روی همان build: عنوان «داشبورد»، PAPER، صفر بلوک خطا).
- [x] `month_scan.py` و زیردستور `research` هم venue-aware شدند (کلید کش و منبع `market` در payload) — بدون تغییر نام‌ها/رفتار برای کریپتو.

## ۳) بازمانده و ریسک‌های_known_
- spread تابلوی بورس اغلب ۰.۲–۱٪ است؛ با `max_spread_pct: 0.30` پیش‌فرض، بیشتر نمادها رد می‌شوند (دروازه‌ی درست، اما config بورس باید آستانه را مستند و بازتعریف کند).
- تأیید `top` آستانه‌ی daily: ۱۰۰۰ رکورد تا ۲۰۲۲-۰۸-۲۰ واقعی؛ قدیمی‌تر نیازمند pagination است (هنوز پیاده نشده).
- حقوق تقدم/اوراق/صندوق: snapshot ۱۰۱۷ ردیفی شامل ۴۱۶ ردیف غیرعادی است و `paperTypes` آن‌ها را حذف نمی‌کرد → فیلتر ISIN (`IRO1/IRO3/IRO5/IRO7` + سریال `0001/0011`) در builder اعمال شد.
- کارمزد واقعی هر حساب با کارگزار فرق می‌کند → مقدار پیش‌فرض `0.325%` در `config.tse.example.yaml` با برچسب «تأییدنشده» آمده است.
- بک‌تست بورس (`backtest.py`/`month_scan.py`) هنوز `GateProvider` را مستقیم صدا می‌زند؛ اتصال کامل بک‌تست به spec یک مرحله‌ی جداست.
- تاریخ نقل‌قول `marketwatch` همیشه `dEven=0` است → timestamp از `marketActivityDEven` جلسه گرفته می‌شود؛ fallback ساعت دیوار حذف شد.
- `week_anchor` یک روز خطا داشت (یکشنبه) → اکنون شنبه‌محور است و تست تجمیع هفتگی آن را ثابت می‌کند.

## ۴) مرزهای ایمنی این فاز
- پیش‌فرض `trading.market: crypto` و `mode: PAPER` باقی می‌ماند؛ هیچ فایل موجودی که کریپتو را می‌خواند حذف/بازنویسی نمی‌شود.
- هیچ route یا دکمه‌ای برای بورس نمی‌تواند سفارش زنده بسازد؛ هیچ اعتبارنامه‌ی کارگزاری/رمز عبور ذخیره یا درخواست نمی‌شود.
- هیچ عدد ساختگی (کارمزد، گام قیمت، قیمت) تولید نمی‌شود؛ نبود داده = `null` + پیام صریح.

## ۵) تسک‌های ردیابی‌شده در جلسه
`#1` کشف قرارداد داده — **انجام شد** · `#2` لایه‌ی markets بک‌اند — **انجام شد** · `#3` صفحه‌ی UI بورس — **رندر مرورگری شد** · `#4` تست و عدم‌رگرسیون — ۱۸۹ سبز (۱۵۶ تست قبلی بدون شکست) · `#5` دیدگاه دوم چندتالاری (config.tse.yaml) — انجام شد و از مرورگر راستی‌آزمایی شد · `#6` افت‌کیفیت صادقانه (نبود نقل‌قول/عمق یک‌طرفه) — انجام شد

---

## فاز جهان ۱۰۰ نمادی و نمودار زنده (2026-10-23)

- [x] `tools/build_universe.py`: رتبه‌بندی واقعی CoinGecko (`market_cap_desc`) + تطبیق با جفت‌های tradable گیت، حذف استیبل‌کوین/طلا/نسخه‌های رپ‌شده، نوشتن `universe.json` با منبع و digest. نتیجه: **۱۰۰ نماد**، ۳۰ حذف‌شده، digest `65f5f4cb…`، snapshot `2026-10-23T20:39:25Z`؛ `config.yaml` با جراحی regex و بدون آسیب به کامنت‌ها به‌روزرسانی شد
- [x] موتور مقیاس‌پذیر برای ۱۰۰ نماد: `ThreadPoolExecutor` با سقف `data.max_concurrency=4`، کش فیلترهای نماد با TTL 3600s، ترتیب نتیجه‌ها همان ترتیب allowlist، و قرنطینه‌ی نماد خراب بدون مصرف بودجه‌ی خطای حساب (`test_one_dead_symbol_is_quarantined_without_halting_the_account`)
- [x] چرخه‌ی واقعی ۱۰۰ نمادی (اجرا‌شده، نه ادعا): ۱۰۰ outcome، یک BUY واقعی `BNB_USDT` (۰.۰۶۷ @ ۷۴۲.۶، کارمزد ۰.۰۴۹۸)، `safe_state=false`، صفر خطا، ۵m۳۹s؛ چرخه‌ی دوم: `HOLD` روی BNB، بدون سفارش تکراری، صفر خطا، ۴m۴۴s (کش فیلترها)
- [x] باگ واقعی چرخه (۵۰۰): `analyze()` روی تاریخچه‌ی کوتاه‌تر از EMA۵۰ با `None` قالب‌بندی می‌کرد → سناریوها حالا صادقانه «unavailable» می‌شوند؛ موتور هم خطای غیرمنتظره‌ی استراتژی را per-cycle مهار می‌کند (fail-closed: توقف سفارش جدید، نه ۵۰۰)
- [x] `GET /universe` + کارت «جهان معاملاتی و منبع رتبه‌بندی» در تنظیمات: تعداد، منبع/رتبه‌بندی، زمان snapshot، digest، تطابق با config، فهرست ۱۰۰ نماد و ۳۰ حذف با دلیل
- [x] `GET /market/candles` + نمودار بازار با **کل ۱۰۰ نماد**: جستجو/انتخاب از جهان معاملاتی و کندل زنده‌ی Gate با `provenance` (منبع، پنجره، زمان دریافت، کش ۲۰ثانیه). نماد خارج از allowlist → 404، تایم‌فریم نامعتبر → 400، قطع صرافی → 503
- [x] اسکرول مستقل «تصمیم‌های موتور» (پنل اسکرول با ارتفاع محدود، اسکرول‌بار مشهود، دسترس‌پذیر با کیبورد) و احترام `/decisions` به `limit` (پیش‌فرض ۲۰۰، حداکثر ۱۰۰۰) — قبلاً همه‌ی تصمیم‌ها را برمی‌گرداند و صفحه را تا ۷۳۷۶px می‌کشید (اکنون ۲۲۸۸px)
- [x] `/config` اکنون `max_open_positions` را می‌دهد (کارت تنظیمات قبلاً «داده‌ای موجود نیست» نشان می‌داد)
- [x] `Cache-Control: no-store` برای shell و `immutable` برای assetهای هش‌شده تا reload ساده، build تازه را بگیرد
- [x] راستی‌آزمایی مرورگری (Edge CDP): داشبورد ۱۴۴۰×۹۰۰ و موبایل ۳۹۰×۸۴۴ بدون overflow افقی، صفر خطای کنسول/اکسپشن؛ انتخاب SOL از ۱۰۰ نماد → ۱۷۹ کندل بسته، منبع gate:SOL_USDT:4h؛ فیلتر «sol» → «۱ از ۱۰۰ نماد»


## انجام‌شده و راستی‌آزمایی‌شده
- [x] #1 موتور PAPER قابل‌اجرا + Risk Gate قطعی (۴۸ تست اولیه)
- [x] #2 اتصال داده‌ی واقعی (gateio/mexc از طریق پروکسی محلی؛ `check: PASS`)
- [x] #3 تست‌های ایمنی الزامی (gap/dup/negative/stale/unclosed + عدم مسیر LIVE در API)
- [x] #4 چرخه‌ی paper + گزارش روزانه در `state/reports/`
- [x] #5 `src/cat/datasets.py` — CSV/OHLCV import، quality، resample، مقایسه‌ی منابع (تست: `tests/test_datasets.py`)
- [x] #6 `src/cat/research.py` — split سه‌بخشی، grid سقف ۶۰، walk-forward، دروازه‌ی پذیرش ضد-overfit (تست: `tests/test_research.py`)
- [x] #7 `src/cat/api.py` + `serve.py` — FastAPI طبق `docs/API_CONTRACT_FA.md`؛ تست صریح: هیچ مسیری LIVE را فعال نمی‌کند
- [x] #8 پوسته‌ی `web/` — Vite+React+TS سخت‌گیرانه، RTL فارسی، نمودارهای SVG دست‌ساز، Dashboard/Reports
- [x] mirror رویدادهای engine (decision/order/fill/trade/snapshot) به Store از یک مسیر مشترک idempotent (`src/cat/mirror.py`)؛ ثابت شد دوبار‌شماری رخ نمی‌دهد
- [x] رفع باگ قراردادی: `/runs/walk-forward` اکنون `dataset_id` واردشده را می‌پذیرد (تست: `test_walk_forward_runs_on_imported_dataset`)؛ شواهد زنده: ۲۰۰ OK روی ۴۰۰ روز داده‌ی واقعی، داوری صادقانه «rejected»، ۱۲ معامله‌ی out-of-sample، قهرمان ماند
- [x] رفع باگ: `params_from_dict` دیگر روی کلیدهای هویتی (`version/name/note`) TypeError نمی‌دهد و NaN/inf را رد می‌کند (لایه‌ی research هم کلید `version` در grid را صریحاً 422 می‌کند — تست: `test_walk_forward_refuses_identity_keys_in_grid`)
- [x] رد شدنِ صادقانه‌ی داده‌ی ناکافی: ۳۶۰ کندل برای سه‌بخشیِ حداقل ۱۲۰تایی رد می‌شود (تست: `test_walk_forward_refuses_datasets_too_small_for_honest_splits`)
- [x] زیردستورهای CLI: `serve`, `import-csv`, `fetch-history`, `datasets`, `quality`, `runs`, `versions`, `research`
- [x] build فرانت: `tsc --noEmit` صفر خطا، `vite build` موفق (۴۰۰KB JS / ۱۲۳KB gzip)
- [x] فایل CI: `.github/workflows/ci.yml` — pytest + دروازه‌ی پیش‌فرض LIVE در CI + زنجیره‌ی audit + tsc + build + artifact
- [x] مخزن خصوصی GitHub ساخته و push شد: `github.com/mrabolfazl13/crypto-agentic-trader-kit` (main = 8bed332؛ CI روی هر push اجرا می‌شود — وضعیت اجرا از این ماشین قابل‌مشاهده نیست: gh نصب نیست و ابزار Actions در MCP موجود نیست)
- [x] صفحه‌ی «کیفیت مجموعه‌داده» و نمایش شکاف/پوشش در تحلیل (`web/src/pages/Data.tsx`, `Analysis.tsx`)

## انجام‌شده و راستی‌آزمایی‌شده
- [x] #9 + #11 راستی‌آزمایی مرورگری ۱۳ مسیر در Edge هدلس (CDP 9344): هر ۱۳ مسیر PASS — RTL/fa، صفر خطای کنسول، صفر خطای شبکه؛ ۳ مسیر در نمای موبایل (390px) هم PASS. بلوک «هیچ استراتژی سودآور تأیید نشده است» در `/versions` همان وضعیت صادقانه‌ی طراحی‌شده است، نه خطا
- [x] باگ واقعی که فقط در مرورگر پیدا شد: deep-linkهای SPA (مثل `/portfolio`) از StaticFiles بدون fallback 404 می‌گرفتند → `SpaStaticFiles` در `src/cat/api.py` shell را برای مسیرهای client-side برمی‌گرداند و assetهای گمشده همچنان 404 صادقانه می‌دهند (تست: `test_spa_fallback_serves_shell_for_client_routes_but_assets_404`)
- [x] سه ناسازگاری قرارداد هدر داشبورد (همه با خروجی واقعی مرورگر پیدا شدند): ۱) نشان «آخرین داده» از فیلدهایی می‌خواند که وجود نداشتند → `/health` حالا `last_data_at` (تازه‌ترین کندل ذخیره‌شده) و `last_cycle_at` (آخرین اسنپ‌شات) می‌دهد؛ ۲) `kill_switch` در فرانت به‌صورت object خوانده می‌شد در حالی که بک‌اند boolean می‌دهد — اگر kill switch فعال می‌شد هدر باز «خاموش» نشان می‌داد؛ اصلاح شد (تست‌های kill_switch سبز ماندند)؛ ۳) `/portfolio` حالا `updated_at` می‌دهد که تایپ فرانت از قبل انتظارش را داشت؛ «آخرین به‌روزرسانی حساب» و «آخرین کندل ذخیره‌شده» حالا دو مفهوم جدا و صادقانه‌اند (تست‌ها: `test_health_last_data_at_tracks_newest_ingested_candle` و …)

## بعدی
- [ ] تست یکپارچگی end-to-end: import → backtest → walk-forward → پذیرش/رد نسخه → گزارش (اسکلت در `tests/test_engine_integration.py` موجود است؛ زنجیره‌ی کامل با API گسترش یابد)
- [ ] اجرای paper چندروزه برای دروازه‌ی B (بازیابی پس از restart)
- [ ] جدول بک‌تست در عرض‌های باریک کمی بریده می‌شود (اسکرول افقی دارد) — بهبود واکنش‌گرایی اختیاری

## بدهی‌های فنی ثبت‌شده
- [ ] grid کامل ۶۰تایی در walk-forward همگام ~۳–۵ دقیقه طول می‌کشد؛ برای UI باید streaming/سقف کندل/کار پس‌زمینه اضافه شود
- [ ] `indicators.py` روی داده‌ی بزرگ O(n·window) است؛ برای 730 روز قابل قبول، برای چند نماد/چند پنجره هم‌زمان باید کش شود
- [ ] `realized_vol_pct` و آستانه‌های regime روی تایم‌فریم‌های دیگر کالیبره نشده‌اند (فقط 4h/1d)
- [ ] slippage در بک‌تست ثابت فرض می‌شود (`assumed_spread_pct`)؛ داده‌ی order book تاریخی نداریم و این محدودیت باید در UI ذکر شود
- [ ] Sharpe/Sortino خام‌اند (بدون نرخ ریسک‌free و با فرض iid)؛ برچسبش باید در همه‌جا بماند

## هرگز انجام نمی‌شود (مگر مالک صریحاً بخواهد و دروازه‌ها سبز باشند)
- فعال‌کردن LIVE، افزایش سقف ریسک/بودجه، مارجین/اهرم/برداشت، اتصال به حساب واقعی

---

# فاز پروفایل‌های سود و زیان + نمایش در داشبورد (2026-10-24)

هدف: پاسخ به یک سؤال مشخص با داده‌ی واقعی — «هر مرحله‌ی توسعه چه سودی داد و چه زیانی؟» — و همان سنجش برای بورس تهران؛ بدون ادعای سود و بدون مسیر سفارش.

## موتور `src/cat/profiles.py`
- [x] **نردبان ۶ مرحله‌ای، هر مرحله یک تغییر**: `baseline-trend` → `momentum` → `momentum-horizon` → `momentum-vol-horizon` (کف نوسان ۱٫۲٪ = ۲× هزینه‌ی رفت‌وبرگشت) → `breakout-vol-horizon` → `trend-horizon`. پارامترها frozen و digest-دار؛ مرحله‌ی بعد فقط یک چیز را عوض می‌کند.
- [x] **داوری واقعی**: نامزد فقط روی نیمه‌ی اول (train) انتخاب می‌شود؛ ترفیع فقط اگر نیمه‌ی دوم (validation) هم با حداقل ۱۵ معامله، PF ≥ ۱ و سود خالص مثبت باشد. قاعده‌ی «پایدار»: PF کل ≥ ۱٫۲، ≥ ۲۰ معامله، سود مثبت در کل و هر دو نیمه. تقسیم بر اساس **زمان خروج** است، نه ورود.
- [x] **خروج افق زمانی** یک‌بار در `strategy.propose` (`exit_horizon`) پیاده شد و همان کد در paper، month-scan و profiles استفاده می‌شود. روی ۴h، افق «یک روز» = ۵ کندل (۲۰h) و افق «کوتاه» = ۱ کندل (۴h)؛ روی 1h کوتاه = ۳ کندل (۳h). عدد کمتر از یک کندل هرگز ادعا نمی‌شود.
- [x] **باگ واقعی که دو مرحله را صفر کرده بود**: `functools.partial` پارامترهای ورودیِ مرحله (`min_atr_pct`) را به `propose()` نیز پاس می‌داد؛ هر نماد با پوزیشن باز `TypeError` می‌خورد و بی‌صدا skip می‌شد. اصلاح با `_exit()` که فقط قرارداد مشترک ریسک/خروج را فوروارد می‌کند + تست رگرسیون (`test_entry_only_stage_overrides_do_not_break_an_open_position`).
- [x] **باگ زمان‌سنجی معامله‌ی باقی‌مانده**: خروج `end_of_test` روی open آخرین کندل ثبت می‌شد و نگه‌داشت ۰h می‌داد؛ حالا روی close همان کندل ثبت می‌شود.
- [x] **صفر معامله = صفر نسبت**: هر جا معامله‌ای نیست، PF/برد/میانه `null` و در UI «داده‌ای نیست» است؛ هیچ نسبت ساختگی ساخته نمی‌شود.

## داشبورد (مشترک کریپتو و بورس)
- [x] `web/src/api/profilesClient.ts` (قرارداد سخت) + `web/src/components/ProfileBoard.tsx` (تخته‌ی مشترک): انتخاب اجرای ثبت‌شده، KPI خالص/ناخالص/زیان/کارمزد/برد/PF/نگه‌داشت، نمودار P&L هر مرحله (زیان چپ، سود راست)، جدول نردبان با Δ نسبت به مرحله‌ی قبل، جدول train/validation، نمودار سبد، بهترین/بدترین نماد، دلیل خروج و فید اسکرول‌شده‌ی معامله‌ها.
- [x] مسیر مستقل `/profiles` (کریپتو) + همان تخته داخل `/tse` با عنوان «پروفایل‌های سود و زیان بورس تهران».
- [x] **راستی‌آزمایی مرورگری (Edge/CDP)**: `/profiles` و `/tse` در ۱۴۴۰×۹۰۰ و ۳۹۰×۸۴۴ — صفر استثنا، صفر خطای کنسول، صفر درخواست ناموفق، `overflow افقی = 0`؛ ۶ نوار P&L، ۴ جدول، ۴۰۰ ردیف معامله در صفحه‌ی کریپتو؛ تخته‌ی بورس با ۳۹ نماد و ۸۵ ردیف معامله رندر شد.
- [x] **بازبینی API**: `GET /api/v1/profiles?market=crypto|tse` فقط اجراهای همان بازار را می‌دهد (باگ فیلتر در سرورِ کهنه بود، نه در کد؛ سرور تازه‌سازی و تأیید شد). اجرای ناشناخته/مسیر traversal → 404، بازار ناشناخته → 400.

## شش اندازه‌گیری واقعی (همه ذخیره‌شده در `state/research/profiles/`)
هر نماد یک حساب مستقل؛ سرمایه‌ی کریپتو ۱٬۰۰۰ USDT و بورس ۱٬۰۰۰٬۰۰۰٬۰۰۰ ریال. کارمزد ۰٫۱٪ + اسلیپیج ۰٫۲٪ (کریپتو) و ۰٫۳٪ اسپرد (بورس).

| # | بازار/تایم‌فریم/بازه | نامزد (train) | ترفیع | خالص هر مرحله (USDT مگر ذکر شود) | نتیجه |
|---|---|---|---|---|---|
| ۱ | کریپتو 4h · ۶۰ روز · افق روز | momentum | خیر | baseline −5.85 · momentum −14.78 · momentum-horizon −111.31 · vol همان · breakout −33.90 · trend −4.93 | همه منفی؛ هیچ مرحله‌ی پایدار |
| ۲ | کریپتو 4h · ۳۰ روز · افق روز | breakout-vol | خیر | −7.56 · −247.84 · −186.40 · −186.40 · −24.52 · −3.01 | همه منفی؛ هیچ پایدار |
| ۳ | کریپتو 1d · ۶۰ روز · افق روز | momentum | خیر | −2.30 · **+115.12** (PF 1.41، نگه‌داشت میانه ۱۵۶h) · −67.77 · −67.77 · −3.42 · −1.00 | train منفی، validation مثبت → ناپایدار |
| ۴ | کریپتو 4h · ۶۰ روز · افق کوتاه (۴h) | momentum | خیر | −5.85 · −14.78 · −534.18 (۲٬۵۷۳ معامله) · −534.18 · −152.21 · −14.81 | افق ۴h بعد از هزینه‌ها زیان‌ده |
| ۵ | کریپتو 1h · ۶۰ روز · کوتاه (۳h) · ۳۰ نماد نقدشونده | baseline-trend | خیر | **+23.22** (۲۰۵، PF 1.16، ۷h) · **+78.78** (۵۷۵، PF 1.14، ۸h) · −186.93 (۳h) · −186.93 · −86.12 (۳h) · −46.27 (۳h) | سود فقط در train؛ validation همه‌ی مراحل منفی → هیچ ترفیع |
| ۶ | بورس تهران 1d · ۱۲۰ روز | breakout-vol-horizon | **بله** | momentum **+22.10M IRR** (۵۱، PF 1.28، ۱۴۴h) · horizon −69.25M · breakout **+7.68M** (۴۱، PF 1.31، ۲۴h؛ train +3.32M / val +4.36M) | تنها مرحله‌ای که هر دو نیمه مثبت بود؛ **شکننده** |

**دو واقعیت که نباید پنهان شوند:**
1. **افق ۲–۳ ساعت روی 1h سود را نابود می‌کند**: momentum با نگه‌داشت ۸h خالص +۷۸٫۸ USDT داد، اما همان ورودها با سقف ۳ ساعت به −۱۸۶٫۹ رسیدند. با کارمزد+اسلیپیج ۰٫۶٪ رفت‌وبرگشت، معامله‌ی ۲–۳ ساعته در این نمونه لبه‌ی قابل‌اتکا ندارد.
2. **ترفیع بورس شکننده است**: اجرای اول با ۳۵/۴۰ نماد، breakout را با ۱۹ معامله‌ی train و validation منفی رد کرد؛ اجرای دوم با ۳۹/۴۰ نماد به ۲۰ معامله و validation مثبت رسید و ترفیع گرفت. یک تفاوت ۴ نمادی مرز را عوض می‌کند → «نامزد قابل بررسی»، نه لبه‌ی اثبات‌شده. ضمناً بورس فقط کندل روزانه دارد؛ هدف «۲–۳ ساعت» در آن **ساختاراً ممکن نیست** و کف افق ۲۴ ساعت است.

## دور چهارم و پنجم پژوهش + انتخاب استراتژی در بک‌تست (۲۰۲۶-۱۰-۲۴)
- [x] **دور چهارم:** پنج خانواده روی 1d/۳۶۵ روز بدون افق؛ `breakout-fresh` (+45.52، PF 1.162) و `momentum-fresh` (+71.14، PF 1.433) کل بازه مثبت شدند اما هر دو در نیمه‌ی اول منفی بودند؛ ترفیع خیر. زمینه: میانه‌ی buy&hold نیمه‌ی اول −44.89٪ و نیمه‌ی دوم +4.52٪.
- [x] **رفع پوشش داده‌ی تأیید:** `higher_days` به `max(300, days + warmup + 90)` تغییر کرد تا پنجره‌ی ۳۶۵ روزه ۶۵ روز اول را بی‌تأیید نگذارد؛ تست رگرسیون `test_confirm_series_covers_the_whole_window_plus_warmup`.
- [x] **دور پنجم (پیش‌ثبت‌شده):** سه جفت control/regime با تنها تغییر `require_regime=True`؛ گیت روی ۳۱٬۱۴۵ از ۳۱٬۳۰۵ کندل روزانه `HIGH_VOLATILITY` را می‌بندد و ورودها را به ۰/۰/۳ (روزانه) و ۰/۵/۷ (۴ ساعته) می‌رساند. **فرضیه رد شد؛ مراحل کم‌معامله اندازه‌گیری‌ناپذیرند، نه پاس.**
- [x] تست‌ها: `225 passed`؛ شامل تست‌های گیت رژیم (فقط سوییچ، ورود در رژیم منطبق)، دور ۴، جفت‌های دور ۵ و پوشش سری تأیید.
- [x] **API بک‌تست با خانواده‌ی نام‌دار:** `strategy` + `rsi_reset` با اعتبارسنجی قبل از fetch و برچسب `pullback_rsi|...`؛ انتخابگر استراتژی و RSI در فرم `/backtests`.
- [x] **استقرار:** `src/cat`، `dist` تازه، گزارش‌های r4/r5 روی سرور؛ `trade_backend` بازسازی و nginx ری‌استارت شد. تأیید مستقر: `/api/v1/profiles` شامل r4/r5، اجرای `pullback_rsi` از UI با ۲۰۰ و برچسب درست، صفر خطای کنسول و صفر overflow در دسکتاپ/موبایل.

## استقرار (سرور `2.189.255.225` · کاربر `ubuntu`)
- [x] **استقرار ۲۰۲۶-۱۰-۲۴ (دور ۲/۳ + داوری اصلاح‌شده):** `src/` و `trader.py` روی `/home/ubuntu/trade/backend` جایگزین و `docker compose build trade_backend` + `up -d --no-deps trade_backend` انجام شد (نسخه‌ی قبلی در `/tmp/trade-src-backup-*`). پوسته‌ی تازه (`index-D17H10N_.js`) در `/home/ubuntu/trade/frontend/dist` و هر ۱۱ گزارش کریپتو در `state/research/profiles/` قرار گرفت.
- [x] **نکته‌ی mount:** حذف و بازسازی پوشه‌ی `dist` باعث شد bind mount کانتینر `futsal_nginx` به inode حذف‌شده اشاره کند و مسیرها 403/404 بدهند؛ با `docker restart futsal_nginx` mount دوباره برقرار شد. در استقرار بعدی مستقیم داخل همان پوشه کپی کنید یا بعد از جابه‌جایی، nginx را restart کنید.
- [x] تأیید: `/`، `/profiles`، asset هش‌دار، `/api/v1/health` و `/api/v1/profiles` روی HTTP و HTTPS = 200؛ جزئیات API: ۶۰ روزه `promoted=True` (۲۸۷/+29.02) و ۱۲۰ روزه `promoted=False` با متن «train مثبت نبود»؛ مرورگر واقعی دسکتاپ/موبایل بدون خطا و سرریز؛ `school` و `salon` هر دو 200 (بدون رگرسیون).
- [x] تصویر `trade-backend` از `deploy/Dockerfile` (python:3.12-slim، `uvicorn cat.api:create_app`، فقط PAPER) و سرویس `trade_backend` در `/home/ubuntu/docker-compose.yml`؛ **nginx جدا ساخته نشد** — همان `futsal_nginx` با mount جدید `./trade/frontend/dist` بازسازی شد.
- [x] بلوک `trade.absadeghi.ir` در `/home/ubuntu/nginx/nginx.conf` (قبل از default): روت = SPA استاتیک، `/api/` → `trade_backend:8000`؛ `nginx -t` سبز و `docker compose up -d --no-deps nginx` بدون دست‌زدن به سرویس‌های دیگر.
- [x] تأیید از داخل سرور: `Host: trade.absadeghi.ir` → `/api/v1/health` = 200 (`PAPER`، `live_trading_enabled=false`)، `/` = shell فارسی، `/profiles` = deep-link درست، TLS با SNI = 200؛ `school.absadeghi.ir/nginx-health` هم بعد از بازسازی 200 ماند.
- [x] گواهی اختصاصی `trade.crt/key` (self-signed، SAN = `trade.absadeghi.ir` + IP) در `/home/ubuntu/nginx/ssl/`؛ فقط بلوک trade به آن اشاره می‌کند و گواهی مشترک `edge` دست‌نخورده ماند. `nginx -t` سبز، `nginx -s reload` انجام شد.
- [x] تأیید از این ماشین با `--resolve trade.absadeghi.ir:443:2.189.255.225`: گواهی سرو‌شده SAN درست دارد؛ `/`، `/profiles`، `/api/v1/health` و `/api/v1/profiles?market=tse` همه 200؛ `school` و `salon` هم 200 (بدون رگرسیون).
- [x] بررسی مرورگر واقعی روی همین vhost (Edge headless + `--host-resolver-rules`): `/profiles` دسکتاپ و موبایل و `/tse` دسکتاپ — **صفر** خطای کنسول/شبکه، **صفر** سرریز افقی، اعداد واقعی از API سرور (۶ نوار سود/زیان، ۴/۶ جدول، برداشت TSE).
- [x] **رفع اجرانشدن بک‌تست داشبورد (۲۰۲۶-۱۰-۲۴):** `COPY schemas` در Dockerfile، هم‌ترازکردن سقف کندل تأیید API با موتور (۸۰→۶۰)، پیش‌فرض «دریافت بازه از صرافی» در فرم، تست رگرسیون پنجره‌ی ۶۰–۷۹ کندلی؛ `217 passed`؛ تصویر بازسازی و UI مستقر با اجرای واقعی ۶۰ روزه تأیید شد.
- [ ] **تنها قدم بیرونی**: رکورد DNS `A trade.absadeghi.ir → 2.189.255.225` در Cloudflare (proxied). رکورد وجود ندارد و توکن/DNS API روی این ماشین یا سرور در دسترس نیست؛ بدون آن نام `trade` resolve نمی‌شود (همین حالا با Host header کار می‌کند).


---

# حلقه‌ی بهینه‌سازی خودکار + پنل `/optimizer` (2026-10-24)

- [x] `src/cat/optimizer.py`: شش فرضیه‌ی پیش‌ثبت‌شده، صف تطبیقی بر اساس دلیل رد، وضعیت JSON پایدار، توقف/ادامه، فقط یک حلقه‌ی فعال، خروجی CSV/JSON.
- [x] اندازه‌گیری با همان `research.run_research` و دروازه‌ی `acceptance`; قهرمان فقط با داوری پذیرفته عوض می‌شود.
- [x] API: `GET /optimizer`، `GET /optimizer/{id}`، `POST /optimizer/start|stop|resume`، `GET /optimizer/{id}/export`.
- [x] پنل `/optimizer`: فرم شروع در Modal، فهرست اجراها، KPI، جدول آزمایش‌ها با دلایل رد و درس، توقف/ادامه، خروجی‌ها، تازه‌سازی ۳ ثانیه‌ای.
- [x] تست‌ها: `tests/test_optimizer.py` (۷ تست، شامل ردمجموعه‌داده کوچک با کف تقسیم صادقانه) + کل مجموعه `232 passed`.
- [x] typecheck و build وب سبز.
- [x] استقرار backend/پنل روی سرور و راستی‌آزمایی مرورگری شروع/توقف/ادامه/خروجی (دو اجرای واقعی با نتیجه‌ی یکسان، صفر خطا/سرریز).
- [x] ثبت صادقانه: هیچ لبه‌ی پایدار اثبات نشده؛ LIVE خاموش می‌ماند.

---

# حلقه‌ی خودکار: شش دور اجراشده + رفع باگ رتبه‌بندی (۲۰۲۶-۱۰-۲۴)

معیار هر آیتم: کد + تست سبز + عدد ثبت‌شده از اجرای واقعی. هیچ معیاری شل نشد.

- [x] رفع باگ ابزار سنجش: `_rank_key` نسخه‌ی بی‌معامله را ۰٫۰٪ می‌شمرد و آن را بر ضرردها ترجیح می‌داد → سه آزمایش دور اول بی‌نتیجه ماند. حالا بی‌معامله آخر از همه می‌آید؛ تست: `test_silent_variant_never_outranks_a_measured_loss`.
- [x] دور ۱ (۶ فرضیه، BTC/4h · ۴۰۰ روز · `ds-ex-e44bf6ffb4`): ۶/۶ رد، همه اندازه‌گیری‌پذیر پس از رفع (−۰٫۱۲٪…−۰٫۴۴٪، ۰ از ۳ فولد سودآور). `opt-019f363a37bd` (قبل از رفع) و `opt-86952a70d86b` (بعد از رفع).
- [x] دور ۲ (۶ فرضیه‌ی تازه‌ی پیش‌ثبت‌شده از شواهد دور ۱ — `start_at=holding-minimum`): ۵ فرضیه دقیقاً مقادیر پایه را انتخاب کردند (knobهای ورود بی‌اثر)؛ تنها `exit-asymmetry` عددش عوض شد: −۰٫۰۳۸٪ / PF ۰٫۶۵۷ / ۱۰ معامله → همچنان رد. `opt-91b7ac6887c8`.
- [x] دور ۳ (بازتر کردن براکت خروج؛ تفکیک stop از target): −۰٫۱۲۲٪ (۱ از ۳ فولد) و −۰٫۱۶۹٪؛ `atr_stop_mult=3.0/3.6` ورود را **صفر** می‌کند (۰ معامله) → آن «بهبود» تدریجی نبود. `opt-290167898ce2`.
- [x] زیرساخت دورها: `start_at` در API/CLI loop (فقط فهرست پیش‌ثبت‌شده، غیر آن ۴۲۲)، `GET /optimizer` حالا خود فرضیه‌ها را برمی‌گرداند، پنل انتخابگر «شروع از فرضیه» با ۱۶ گزینه (راستی‌آزمایی Edge CDP: صفر خطا/سرریز)، سقف آزمایش ۲۰. تست‌ها: `236 passed`، `tsc` صفر خطا، build موفق.
- [x] آزمون حساسیت نماد (ETH/4h · ۲۱۹۰ کندل · ۳۶۵ روز): دور ۱ کامل → ۶/۶ رد (−۰٫۰۹۷٪…−۰٫۴۰۹٪، ۰ از ۳ فولد). `opt-3a17c00de733`.
- [x] **تکرارناپذیریِ تنها «نزدیک‌به‌صفر»:** `exit-asymmetry` روی ETH = −۰٫۲۶۹٪ / PF ۰٫۱۰۸ → آن عدد BTC نویز پنجره بود. `opt-74d833821fa1`.
- [x] دور ۴ (کلیدهای متنی/بولی که قبلاً قابل‌جستجو نبودند): `regime_gate=RANGING` روی BTC اولین مثبت‌ها: **+۰٫۹۵٪ / PF ۲٫۲۳۶ / افت ۰٫۱۷۷٪** اما فقط ۴ معامله (زیر کف ۸) → رد به‌عنوان نمونه‌ی ناکافی. `HIGH_VOLATILITY` gate، `conflict-guard-off` و `multi-signal` بی‌اثر (پایه انتخاب شد). `opt-b866c2e42296`.
- [x] دور ۵ (`vol_threshold` ۱/۲/۳ برای شکستن قفل HIGH_VOLATILITY): −۰٫۳۲۷٪، ۱۳ معامله، ۰ از ۳ فولد. همان اجرا.
- [x] دور ۶ (پیش‌ثبت‌شده: باند RSI بازتر در گیت RANGING تا به ۸ معامله برسیم): همان ۴ معامله و همان اعداد → کمبود نمونه از کمیابی رنج است، نه از باند. `opt-b5c2125f4be2`.
- [x] آزمون بین‌نمادیِ همان فرضیه: `regime-gate=RANGING` روی ETH = −۰٫۱۱۵٪ / PF ۰٫۰ / ۸ معامله → مثبت BTC تکرار نشد و رد شد. `opt-900597bf7dd2`.
- [x] **آزمون بسپاری‌پذیری روی ۸ نماد مستقل ۴۰۰ روزه** (SOL/BNB/XRP/ADA/DOGE/LINK/AVAX/ETH؛ هر کدام ۲۴۰۰ کندل، coverage 100%، gaps 0): فرضیه‌ی `regime-gate-ranging` هشت‌بار، یکی‌یکی. نتیجه: در ۳ نماد grid همان پارامترهای فعلی را انتخاب کرد (آزمون انجام نشد)، در ۳ نماد گیت RANGING ورود را **۰** کرد (fail-closed)، در SOL/ETH با ۱ و ۷ معامله زیر کف نمونه ماند و هیچ‌جا مثبت نشد → **+۰٫۹۵٪ BTC رسماً نویز پنجره است.** اجراها: `opt-7117c803ddff` `opt-7dbba0c79bdf` `opt-507fbc72f5fa` `opt-d1668047915a` `opt-5a71706752b2` `opt-e2014df16e36` `opt-5df31ae5ef54` `opt-eccd5ee9e075`.
- [x] رفع باگ گزارشِ «آزمون بی‌اثر»: `acceptance` حالا صریح می‌گوید `candidate is the unchanged champion … nothing new was tested` و `learning_text` دسته‌ی `no_op` را ثبت می‌کند؛ با ری‌استارت سرور و اجرای مجدد BNB/LINK از مسیر API راستی‌آزمایی شد (اعداد مو‌به‌مو یکسان). تست‌ها: **۲۳۸ passed**.
- [x] **اتصال حلقه به خانواده‌های `strategy_lab`** (دور ۷): `strategy` در `evaluate`/`walk_forward`/`grid_search`/`run_research`، پیشوند خانواده در برچسب اجرا و `source = grid_search:<family>` در نسخه‌ها، `grid` خالی = «فقط خانواده عوض شود»، مبنای داوری = بیلد فعالِ config (نه بازتولید کاندید)، و `params` قهرمان دیگر دور ریخته نمی‌شود. چهار فرضیه‌ی پیش‌ثبت‌شده (`family-breakout-30`, `family-momentum-20`, `family-pullback-rsi`, `family-trend-v1` به‌عنوان کنترل). اجرا روی BTC/ETH/SOL ۴۰۰ روزه: **۱۲ آزمایش، ۰ پذیرش**؛ `momentum_20` روی BTC با ۱۰ معامله و ۰ از ۳ فولد سودآور، اولین ردِ «با شواهد کافی» بود. `opt-5cc635d5b637` `opt-83a8b4a6a079` `opt-84a3853b8082`. تست‌ها: **۲۴۲ passed**، `tsc` صفر خطا، `vite build` موفق.
- [x] **خروجی خوانا شد (پاسخ به «خروجی‌ها جالب نیست»):** منحنی mark-to-market سرمایه (۹۰ نقطه، نرمال به ۱۰۰) + مرجع خرید‌ونگه‌داریِ همان بازه + منحنی قهرمان در `evaluate`/`_record`؛ `champion_validation` در بدنه‌ی `run_research`؛ جمله‌ی «بازار در همان بازه +x% بود» در درس ثبت‌شده؛ نمای پیش‌فرض **«خلاصه پژوهش»** در پنل با کارت هر آزمایش و نمودار SVG (`EquityChart`)؛ ستون «بازار» در جدول؛ دو ستون تازه در CSV. آزمایش‌های قدیمی صادقانه «منحنی ذخیره نشده» می‌گویند. وارسی: یک آزمایش واقعی (`opt-fded50a95429`، `entry-speed`، رد؛ بازار −۱۳٫۳۷٪ در همان بازه) + Edge CDP دسکتاپ/موبایل (۳ خط نمودار، صفر خطا/سرریز، سقوط محترمانه‌ی اجرای قدیمی). تست‌ها: **۲۴۶ passed**.
- [x] **چرخه‌ی کامل مأموریت تحلیل معاملات (دور ۸):** شواهد تصمیم در بک‌تست (`evidence_json` با مهاجرت درجا در `run_trades`)، ماژول `trade_analysis` (گزارش چندنمادی، تحلیل گروهی روی ۸ بُعد با کیفیت نمونه، کشف حداکثر ۳ فرضیه از شواهد، توضیح دو-لایه‌ی هر معامله)، سه مسیر API + ذخیره‌ی گزارش در `state/research/trade-analysis/`، پنل `/trade-analysis` با بازپخش کندل (ورود/استاپ/هدف) و دکمه‌ی «شروع آزمایش در حلقه». رفع باگ ساختاری: فرضیه‌ی استاپِ بازتر بدون بازکردن هدف هرگز از گات سود/ریسک خالص نمی‌گذشت (`opt-b263c66f6853`: ۰ معامله، fail-closed) → قاعده‌ی جفت‌سازی `target >= 1.5*stop + 1.5` + تست. چرخه‌ی واقعی از UI: ۳ بک‌تست ۴۰۰ روزه‌ی BTC/ETH/SOL (همه خالص منفی: −۰٫۹۷۳٪ / −۰٫۴۷۳٪ / −۰٫۳۱۹٪)، گزارش `ta-637a360c4613` (۲۶ معامله، ۳ نماد)، حلقه‌ی `opt-83cd7ea19c9d`: کاندید استاپ `[2.4, 3.6]` + هدف `[5.2, 7.2]` → ۷ معامله OOS، خالص −۰٫۱۲۲٪، PF ۰٫۰۸۵ مقابل قهرمان ۰٫۲۱۴، ۱ از ۳ فولد → رد صادقانه. تست‌ها: **۲۷۱ passed**، `tsc` صفر خطا، `vite build` موفق، وارسی Edge هدلس دسکتاپ/موبایل با صفر خطای کنسول.
- [x] **کاوش تایم‌فریم 1d با حلقه (بدهی کالیبراسیون، دور ۹):** برنامه‌ی پیش‌فرض ۶ فرضیه روی BTC/1d همه fail-closed (`opt-da73e4e720a8`؛ `regime_filter` ×۱۱۱۹ در `run_rejections`). تشخیص با موتور واقعی: روی 1d توزیع رژیم ۱۱۱۹/۱۱۱۹ HIGH_VOLATILITY (میانه‌ی ATR٪ ۳٫۴۴ مقابل آستانه‌ی ۱٫۵؛ نوسان هرکندلی ۲٫۲۷ مقابل ۱٫۰) در حالی که همان کد روی ۴h توزیع مختلط دارد — قفل، OR دو آستانه‌ی مقیاس-هرکندل است و جاروب تک‌پارامتری اثبات کرد هر آستانه به‌تنهایی بازش نمی‌کند. فرضیه‌ی تولیدشده `daily-regime-scale-calibration` (هر دو آستانه): BTC `opt-65487c3e669a` → ۲۱ معامله، +۰٫۸۳۳٪، PF ۱٫۶۱۸ → رد (PF قهرمانِ خاموشِ 1d در دسترس نیست؛ برش OOS عمیق‌تر −۰٫۵۲۷٪)؛ ETH `opt-642d00771257` → ۲۱ معامله، +۱٫۳۹۳٪، PF ۲٫۰۴۸، افت ۱٫۰۰۴٪ → رد (همان دلیل + افت ۱pp بیشتر از قهرمان ۰٪). نتیجه: کالیبراسیون ورود را برمی‌گرداند اما لبه اثبات نمی‌شود و تغییر آستانه‌های پیش‌فرض یک تصمیم پیکربندی آشکار است، نه کار حلقه. همه‌ی اعداد از مسیر HTTP واقعی؛ بدون تغییر کد؛ ۲۷۱ passed.
- [ ] اجرای چندنورگی paper برای دروازه‌ی B با کد پژوهشی فعلی (بدون تغییر حد ریسک یا بودجه).
- [ ] تست یکپارچگی سرتاسری: دریافت داده → حلقه → پذیرش/رد → خروجی CSV/JSON → پنل، با داده‌ی آفلاینِ تثبیت‌شده.

**مجموع تا اکنون: ۲۹ اجرای حلقه، ۷۱ آزمایش روی ۹ نماد و دو تایم‌فریم (۴۷ پارامتری + ۱۲ خانواده‌ای + ۱ وارسی خروجی + ۲ چرخه‌ی تحلیل معاملات + ۹ آزمایش کاوش 1d)، ۰ پذیرش، قهرمان: هیچ.** دروازه‌ی A همچنان از نظر مهندسی PASS و از نظر edge FAIL است؛ LIVE توجیه ندارد.
