# TASKS

معیار پذیرش هر آیتم: کد قابل‌اجرا + تست سبز + شواهد اندازه‌گیری‌شده. بدون «طرح» به‌عنوان خروجی نهایی.
وضعیت تا 2026-10-23 — مجموعه‌ی تست: **۱۲۷ passed** (`python -m pytest -q`).

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
