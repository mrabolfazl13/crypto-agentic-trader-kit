# EXPERIMENT_LOG — راهنمای لاگ آزمایش‌ها

منبع حقیقت: [`EXPERIMENT_LOG.csv`](EXPERIMENT_LOG.csv). این فایل فقط **قرارداد** را توضیح می‌دهد و
هیچ ردیفی را تکرار نمی‌کند تا دو نسخه از یک عدد وجود نداشته باشد.

## ستون‌ها

| ستون | معنا |
|---|---|
| `date` | زمان ثبت (UTC، ISO) |
| `run_id` | شناسه‌ی یکتای اجرا/ترکیب |
| `code_version` | `git short-sha + quant:<digest>` — کندل محتوای کد/پیکربندی کوانت در زمان اجرا |
| `strategy`, `timeframe` | خانواده و تایم‌فریم |
| `slice` | `train` / `validation` / `test` / `nested_walk_forward` |
| `params_label` | پارامترهای انتخاب‌شده یا برچسب ترکیب |
| `data_start`, `data_end` | بازه‌ی داده |
| `window_start`, `window_end` | پنجره‌ی سنجش واقعی |
| `trades` | تعداد معاملات بسته |
| `net_return_pct` | بازده خالص پس از کارمزد/اسلیپیج |
| `max_drawdown_pct` | DD روی منحنی mark-to-market |
| `effective_drawdown_pct` | DD مؤثر (سقف/مقیاس‌شده) در صورت وجود |
| `profit_factor` | PF |
| `win_rate_pct` | نرخ برد |
| `fees` | مجموع کارمزد |
| `exposure_pct` | درصد زمان در معرض بازار |
| `benchmark_return_pct` | بازده buy-and-hold همان پنجره |
| `excess_return_pct` | `net − benchmark` (pp) |
| `status` | `completed` یا `completed; decision=…; positive_folds_pct=…; folds=…; final_test_read=…` |

## قواعد ثبت (پیش‌ثبت‌شده)

1. هر آزمایش — مثبت یا منفی — یک ردیف می‌گیرد؛ ردیف‌ها حذف/بازنویسی نمی‌شوند.
2. آستانه‌ی پذیرش فقط از `src/cat/quant/loop.py → acceptance` می‌آید؛ تغییرش نیازمند رکورد در `DECISIONS.md`
   **قبل از** اجرا است.
3. `final_test_read=false` یعنی پنجره‌ی نهایی خوانده نشده؛ هر اجرایی که آن را بخواند باید صریحاً ثبت شود.
4. `code_version` و `data_version` (در JSON هر اجرا) باید با هم ثبت شوند تا بازتولید ممکن باشد.
5. جزئیات هر ترکیب در `state/quant/results/<run-dir>/nested-wf-<strategy>-<tf>.json` است؛
   خلاصه‌ی ماشین‌خوان در `progress.json → experiments` بازتولید می‌شود. پوشه‌ی `state/` در git ignore است،
   پس فایل‌های تفصیلی فقط روی همین ماشین/بکاپ محلی‌اند و **CSV رکورد قابل‌حمل** است.

## بازتولید

```bash
# گیت‌دار روی کل دریاچه‌ی ۷ ماهه (نمونه)
python tools/quant_nested_walk_forward.py --out-dir state/quant/results/nested_walk_forward_guarded

# بازسازی خلاصه‌ی ماشین‌خوان از فایل‌های واقعی
python tools/progress_snapshot.py

# شمارشِ گیتِ موتورِ مستقر (D-018/D-019؛ تشخیصی، فقط‌خواندنی، بدون نوشتن در CSV)
for tf in 15m 1h 4h; do
  python tools/paper_signal_census.py --timeframe $tf --symbols 30 --count-from 2026-05-01 --count-to 2026-08-01 --out state/quant/results/census_matched_$tf
  python tools/paper_signal_census.py --timeframe $tf --normalize-to 4h --symbols 30 --count-from 2026-05-01 --count-to 2026-08-01 --out state/quant/results/census_norm4h_$tf
done
# warm-up باید ۳۰۰ بماند (پیش‌فرض ابزار = limit در GateProvider.klines؛ F-020)

# فانلِ تصمیمِ خودِ حلقه (D-020 قدمِ الف؛ فقط‌خواندنی، داوری را تغییر نمی‌دهد)
# اسنپ‌شات از کانتینرِ سرور گرفته می‌شود: docker exec trade_backend sqlite3 /app/state/platform.sqlite3 "VACUUM INTO '/tmp/snap.sqlite3'"
# سپس docker cp + scp. خروجیِ 2026-10-27T18:33:25Z: 1300 سطر ⇒ regime_filter 93.15%، executed:buy 0.08%
# منبعِ فانل جدولِ decisions است، نه run_rejections (آن سطرهای grid/walk_forward هستند؛ F-021)
python tools/paper_gate_b.py --db state/reports/server_platform_snapshot.sqlite3 --out state/reports/gate_b_evidence_server.json

# بازپخشِ فرکانسِ کدِ مستقر (D-020 قدمِ ب و پ؛ فقط‌خواندنی، بدونِ سطرِ CSV چون این داوریِ استراتژی نیست)
for tf in 4h 1h 15m; do   # هر-میله = پروتکلِ نخستِ D-020 (تواترِ تصمیمِ بیشتر از چرخه‌ی مستقر)
  python tools/paper_frequency_replay.py --timeframe $tf --symbols 30 --from 2026-05-01 --to 2026-08-01 --out state/quant/results/freq_$tf
done
for tf in 4h 1h 15m; do   # تیکِ 3 ساعته = ساعتِ واقعیِ چرخه‌ی مستقر (15m هر 12 میله، 1h هر 3، 4h هر میله)
  python tools/paper_frequency_replay.py --timeframe $tf --symbols 30 --from 2026-05-01 --to 2026-08-01 --cadence production --out state/quant/results/freqprod_$tf
done
# برشِ دومِ همان پروتکل (تکرارِ سنجش، پنجره‌ی مستقلِ سوم نیست)
for tf in 4h 1h; do
  python tools/paper_frequency_replay.py --timeframe $tf --symbols 30 --from 2026-06-01 --to 2026-08-31 --out state/quant/results/freq_${tf}_window2
done
# عددِ داوریِ هر اجرا در summary.single_account_schedule (یک حساب، یک موقعیت، سقفِ روزانه، cooldown)
# window_bars زیر ۳۰۰ را ابزار رد می‌کند (F-020)؛ نگاشتِ نامِ نماد به allowlist با تست قفل شده (F-022)


# پنجره‌ی مستقلِ سوم (D-021): آرشیوِ روزانه، جدا از دریاچه‌ی ماهیانه
python tools/quant_ingest_daily.py --from 2026-10-01 --to 2026-10-26 --no-proxy     --out state/quant/results/daily_ingest_30.json
# --no-proxy لازم است چون 127.0.0.1:1080 در config پاسخ نمی‌دهد؛ مستقیم کار می‌کند
python tools/quant_daily_crosscheck.py --symbol BTCUSDT --month 2026-09 --days 3     --out state/quant/results/daily_crosscheck.json   # verdict باید identical باشد
# بازتولیدِ بی‌خطرِ manifest: اجرایِ دومِ همان ingest کاملاً resume است (۴ ثانیه، بدون دانلود)
for tf in 4h 1h 15m; do
  python tools/paper_signal_census.py --timeframe $tf --symbols 30 --count-from 2026-10-01       --count-to 2026-10-27 --extra-months 2026-10 --out state/quant/results/census_w3_$tf
  python tools/paper_frequency_replay.py --timeframe $tf --symbols 30 --from 2026-10-01       --to 2026-10-27 --extra-months 2026-10 --cadence production --out state/quant/results/freqprod_w3_$tf
done
# A/B گیت رژیم روی هر دو پنجره (--regime-symbol= فقط در حافظه گیت را خاموش می‌کند)
for tf in 4h 1h; do
  for w in "2026-10-01 2026-10-27 --extra-months 2026-10" "2026-05-01 2026-08-01"; do
    python tools/quant_backtest.py --strategy trend_pullback --timeframe $tf --limit 30 $w         --json state/quant/results/gate_ab/<tag>.json
  done
done
# بازبینیِ مشروطِ زمان: پس از انتشارِ zip ماهیانه‌ی 2026-10 (≈ ۱ نوامبر)
python tools/quant_daily_crosscheck.py --symbol BTCUSDT --month 2026-10 --days 26
# خروجی‌های معیوبِ نخست: state/quant/results/freq_4h_symbolbug_superseded و freq_1h_symbolbug_superseded
```

### D-023 — منحنیِ cadence-هم‌زمانی (K∈{1,2,3,5} روی همان بازپخشِ D-022)

بازتولیدِ K=1 در هر شش سلول: `reproduces_recorded_k1 = true` (0.1848/0.8696/1.0109 برای w1 و 0.0769/1.0385/1.5769 برای w3). کمترین K با عبورِ هر دو پنجره: 1h=2، 15m=2، 4h=∞. در همان K=2 هیچ PF≥1 نیست (0.721/0.272 و 0.811/0.439) و تخصیص لازم دو برابر است. جدولِ کامل در DECISIONS و `state/quant/results/concurrency_sweep_100/concurrency_curve.json`.


### D-024 — سطحِ مطلقِ آستانه (λ ∈ {0.5, 0.75, 1.5, 2.0} + λ=1 از رکورد)

۳۰ خانه (۵ λ × ۳ timeframe × ۲ پنجره)، ۰ گمشده، هر λ با digestِ خودش. λ≤0.75 در **همه‌ی ۱۲ خانه صفر ترید** است
چون `vol_threshold`/`atr_pct_threshold` سقف‌اند (`src/cat/strategy.py:123` ⇒ `HIGH_VOLATILITY`) و پایین‌آوردنشان
دروازه را می‌بندد؛ خانه‌های λ=1.5/2.0 تعداد را تا 2.1538/روز در K=1 بالا می‌برند (۹ خانه از نرخِ Gate B رد می‌شوند)
اما **همه net منفی** و سقف PFشان 0.902. تنها خانه‌ی مثبتِ شبکه λ=1·w1·4h است (PF 1.173، net +1.9721) که روی w3
همان timeframe ⇒ PF 0.0. قاعده‌ی پیش‌ثبت: **۰ از ۱۵** ترکیب ⇒ هیچ λ پیشنهاد نشد و config دست‌نخورده ماند.
جدول کامل: `state/quant/results/lambda_grid/threshold_lambda_grid.json` و `grid_table.txt`.

### D-025 — عرضِ مجاز در برابرِ عرضِ شاهددار (فقط‌خواندنی روی شش خانه‌ی D-022)

هم‌پوشانیِ `trading.symbols` (۱۰۰) با دریاچه (۱۰۰) = **۶۶**؛ ۳۴ نمادِ مجاز هیچ داده‌ی بک‌تست ندارند و ۳۴ نمادِ
دریاچه قابلِ سفارش نیستند. **همه‌ی تریدهایِ ثبت‌شده مجازند** (not_approved = ۰ در هر شش خانه) ⇒ اعدادِ cadence
بزرگ‌نمایی ندارند و رکوردهای D-020/D-022/D-023 نیاز به تصحیح ندارند. هزینه‌ی اهرمِ عرض = تیک‌های ردشده:
۱۲/۲۲۲/۱۰۰ (w1 4h/1h/15m) و ۰/۱۰۰/۳۴ (w3) ⇒ حداکثر ۰.۴۸٪ از تیک‌ها. خروجی:
`state/quant/results/breadth_audit/breadth_audit.json`.

### D-026 — عرضِ *مجاز*: بازپخش روی عینِ allowlistِ مستقر + پوششِ داده‌ی قابل‌دریافت

پروبِ ۱۰۰ نمادِ قابل‌سفارش (`tools/build_allowlist_universe.py`، digest `cf828076fde8f347`) ⇒ **پوششِ ممکن
۶۹ از ۱۰۰**: ۲۸ نماد هیچ آرشیوِ ماهیانه‌ی ۲۰۲۶-۰۳..۰۹ ندارند، ۳ ناقص‌اند (AERO/GRAM فقط ۰۷..۰۹، HYPE فقط ۰۹)
و ۳ تازه‌وارد (ENA/FF/ONDO) دریافت شدند (۲۱ فایل / ۹۲۴٬۴۸۰ ردیف + ۷۸ نماد-روز روزانه / ۱۱۲٬۳۲۰ ردیف؛
`state/quant` = 0.535 GB). دو قفلِ صحت بعد از ingest عیناً بازتولید شدند: w1·4h = 17 / PF 1.173 /
net +1.9721 و w3·4h = 7 / 0.0769 / PF 0.0 / net −1.014 / 390.0.

شش خانه روی ۶۹ نماد (تک‌حسابِ سریالی، تیکِ 3h، کرِدة ۳۰۰): 4h 0.1848 / 1h 0.9022 / 15m 1.0978 (w1) و
0.0769 / 1.1154 / 1.6538 (w3)؛ نسبتِ cadence به سبدِ ۱۰۰تایی = **×1.00 (4h، مجموعه‌ی ترید بیت‌به‌بیت یکسان)،
×1.0375 / ×1.074 (1h)، ×1.086 / ×1.0488 (15m)** ⇒ برآوردِ سنجیده‌نشده‌ی ×1.52 در D-025 جهتِ درست و مقدارِ
بزرگ داشت. کنترلِ پسینِ ۶۶→۶۹ (اعلام‌شده بعد از دیدنِ نتایج) اثرِ قابل‌انجامِ عرض را ×1.0357 تا ×1.1025
اندازه گرفت. منحنیِ هم‌زمانی روی همین سبد همان پاسخِ D-023 را داد: کمترین K=2، 4h تا K=5 هرگز، در K=2
هیچ PF ≥ 1 و تخصیص 10.0٪. قاعده‌ی پذیرش (هر دو پنجره: PF ≥ 1.1 و net > 0) ⇒ **۰ از ۳ timeframe**؛
هیچ عددی به `config.yaml` نرسید. شواهد: `state/quant/results/{freqprod_d026_lock_*,allowlist_breadth/}`،
۱۰ سطرِ `alw-*`، نقص‌ها: F-028 (ردِ اهرم + سقفِ پوشش) و F-029 (سه عددِ DD از حافظه ⇒ قاعده‌ی «اعدادِ docs
فقط از JSON با اسکریپت»). **495 passed** در 42.21s.
