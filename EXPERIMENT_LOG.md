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
# gارددار روی کل دریاچه‌ی ۷ ماهه (نمونه)
python tools/quant_nested_walk_forward.py --out-dir state/quant/results/nested_walk_forward_guarded

# بازسازی خلاصه‌ی ماشین‌خوان از فایل‌های واقعی
python tools/progress_snapshot.py
```
