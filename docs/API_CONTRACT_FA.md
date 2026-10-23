# قرارداد API پلتفرم (v1)

- Base path: `/api/v1` — JSON، UTF-8، بدون احراز هویت (فقط localhost؛ سرویس محلی است).
- هیچ endpoint ای سفارش زنده ایجاد نمی‌کند و هیچ endpoint ای `mode` را به `LIVE` تغییر نمی‌دهد. مسیرهای تغییر حالت در API وجود ندارند.
- همه‌ی پاسخ‌های عددی باید منشأ را مشخص کنند. قاعده‌ی سرزدن: اگر داده‌ای نیست، `null` یا `"no_data"` — هرگز عدد نمونه‌ای.

## اجزای مشترک
```json
{ "provenance": {"kind": "history|simulated|paper|live", "source": "gate|mexc|csv|engine",
                 "window": "2024-10-23T00:00Z → 2026-10-23T00:00Z", "generated_at": "…"} }
```
`kind=history` = داده‌ی خام صرافی/CSV، `simulated` = خروجی بک‌تست، `paper` = چرخه‌های شبیه‌سازی‌شده‌ی زنده‌ی حساب paper.

## خواندن
| متد | مسیر | محتوا |
|---|---|---|
| GET | `/health` | وضعیت سرویس، نسخه‌ی schema، شمارش جداول، `mode` فعلی، kill switch |
| GET | `/config` | پیکربندی قرینه‌شده (بدون مقدار پروکسی/کلید)، `strategy_params`، هشدارها |
| GET | `/portfolio` | equity/cash/unrealized/realized/day_pnl/exposure/drawdown/trades_today/consecutive_errors/fees + `positions[]` |
| GET | `/performance?symbol=&window=` | metrics تجمیعی ledger (win rate, profit factor, avg win/loss, net, fees) با `provenance` |
| GET | `/equity-curve?mode=PAPER` | نقاط `[ts, equity, drawdown_pct]` |
| GET | `/orders?status=open` | سفارش‌ها (paper) |
| GET | `/trades?mode=PAPER&symbol=&outcome=win|loss&limit=&offset=` | تاریخچه‌ی معاملات با فیلتر |
| GET | `/decisions?date=&symbol=&limit=` | تصمیم‌ها + rationale + حکم Risk Gate. `limit` پیش‌فرض ۲۰۰ و حداکثر ۱۰۰۰؛ مقدار نامعتبر `422`. |
| GET | `/universe` | فهرست مؤثر نمادها + snapshot رتبه‌بندی (`universe.json`): منبع، زمان، digest، تعداد، حذف‌شده‌ها و `matches_config`. نبود manifest صریحاً گزارش می‌شود، نه با داده‌ی ساختگی. |
| GET | `/market/candles?symbol=&timeframe=&limit=` | کندل‌های بسته برای یک نماد از جهان معاملاتی، مستقیم از Gate. نماد خارج از `trading.symbols` → `404`؛ تایم‌فریم نامعتبر → `400`؛ صرافی در دسترس نیست → `503`. پاسخ شامل `provenance.source=gate:BASE_QUOTE:tf`، `window`، `fetched_at` و `cached` (کش درون‌پردازشی ۲۰ ثانیه). |
| GET | `/datasets` | فهرست مجموعه‌داده‌ها (origin: `exchange` یا `csv`) |
| GET | `/datasets/{id}/quality` | گزارش کیفیت: شکاف، تکرار، OHLC نامعتبر، کندل باز، stale، تعداد رد شدها |
| GET | `/datasets/{id}/bars` | کندل‌های همان dataset (`limit≤5000`، `overlays=true` برای EMA). مجموعه‌داده نامعلوم `404` است، نه لیست خالی. EMA روی کل سری محاسبه و سپس بریده می‌شود تا مقدار seedشده از پنجره‌ی visible نباشد. |
| GET | `/analysis?symbol=&timeframe=&dataset_id=` | آخرین `analyze()`: regime، سیگنال‌ها، اندیکاتورها، حجم، سناریوها |
| GET | `/runs?kind=backtest|walk_forward|grid&symbol=` | فهرست اجراها + metrics |
| GET | `/runs/{id}` | جزئیات اجرا: metrics، split، trades، rejections، equity curve، params |
| GET | `/strategies` | نسخه‌های استراتژی + نتایج train/validation/test + وضعیت پذیرش |
| GET | `/risk` | محدودیت‌های فعال (risk caps) و مصرف امروز/هفته + وضعیت kill switch |
| GET | `/logs?level=` | رویدادهای سیستم |
| GET | `/reports/{day}` | گزارش روزانه‌ی فارسی (Markdown) |

## نوشتن (همه فقط تحقیق/paper)
| متد | مسیر | محتوا |
|---|---|---|
| POST | `/datasets/import` | `{path|upload_name, symbol, timeframe, origin}` → اعتبارسنجی + ذخیره؛ پاسخ `dataset_id` و quality |
| POST | `/datasets/fetch` | `{symbol, timeframe, days}` → دریافت تاریخ از Gate و ساخت dataset |
| POST | `/runs/backtest` | `{symbol, timeframe, days|dataset_id, params, warmup, assumed_spread_pct}` → اجرای بک‌تست، ذخیره‌ی run، برگرداندن metrics |
| POST | `/runs/walk-forward` | `{symbol, timeframe, days, grid: {param: [values]}, splits}` → ارزیابی walk-forward + تصمیم پذیرش |
| POST | `/cycles/paper` | اجرای یک چرخه‌ی paper (همان `trader.py cycle`)؛ نتیجه‌ی audit و outcomes |
| POST | `/kill-switch` | `{active: true|false}` — فعال/غیرفعال کردن فایل stop (تنها راه «توقف»؛ راه «شروع زنده» وجود ندارد) |

## خطاها
- `422` ورودی نامعتبر (مثل بازه‌ی بدون داده کافی) با پیام فارسی و `detail`.
- `503` داده در دسترس نیست / پروکسی قطع است → سیستم fail-closed می‌ماند و هیچ اجرا/سفارشی شروع نمی‌شود.
- هرگز موجودی API key، token یا پروکسی در پاسخ یا log نمایش داده نمی‌شود.

## جداسازی سه حالت
`research` (بدون سفارش) / `paper` (موجودی فرضی) / `live` (خاموش در این API). فیلد `mode` در همه‌ی رکوردها الزامی است تا UI بتواند «شبیه‌سازی» را از «paper» و «زنده» متمایز نشان دهد.
