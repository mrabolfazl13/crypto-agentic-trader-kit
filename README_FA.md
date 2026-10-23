# کیت عامل هوشمند تحلیل و معامله رمزارز

این بسته یک چارچوب اجرایی برای ساخت Agentic Crypto Analyst/Trader است؛ خودش صرافی یا ربات آماده‌ی متصل به حساب شما نیست. عامل باید بازار را پایش کند، تحلیل قابل‌ردیابی بسازد، در حالت شبیه‌سازی معامله انجام دهد و فقط پس از پیکربندی صریح شما، اجازه‌ی معامله‌ی واقعی در محدوده‌ی از پیش تعیین‌شده داشته باشد.

## اصل مهم
- سود تضمین‌شده وجود ندارد؛ ممکن است تمام سرمایه‌ی اختصاص‌یافته از دست برود.
- پیش‌فرض `PAPER` است. هیچ معامله‌ی واقعی با تنظیمات پیش‌فرض مجاز نیست.
- عامل حق برداشت وجه، انتقال دارایی، تغییر مجوز API یا افزایش خودسرانه‌ی سقف ریسک را ندارد.
- برای شروع، از کلید API جداگانه با مجوزهای فقط خواندن و معامله استفاده کنید؛ مجوز برداشت را فعال نکنید.
- ابتدا بک‌تست، سپس paper trading، سپس اجرای زنده با مبلغ بسیار کم و نظارت فعال.

## محتویات
- `AGENTS.md`: قوانین اصلی عامل و هماهنگی ایجنت‌ها
- `config.example.yaml`: پیکربندی محافظه‌کارانه‌ی نمونه
- `skills/`: مهارت‌های تحلیل، مدیریت ریسک، اجرای سفارش، پایش و ارزیابی
- `docs/`: معماری، پروتکل معاملات زنده، چک‌لیست تست و امنیت
- `schemas/trade_decision.schema.json`: قرارداد ساختاری تصمیم معامله
- `prompts/`: پرامپت سیستم و قالب گزارش
- `src/cat/`: پیاده‌سازی موتور (داده، ریسک، اجرا، بک‌تست، ممیزی، گزارش)
- `tests/`: تست‌های الزامی ایمنی و مهندسی — کاملاً آفلاین
- `tools/`: تشخیص قیف سیگنال و تولید شواهد دروازه‌ی A
- `state/`: حساب paper، audit log، گزارش‌ها (در Git قرار نمی‌گیرد)

## راه‌اندازی پیشنهادی
1. یک مخزن Git بسازید و این فایل‌ها را در ریشه‌ی آن قرار دهید.
2. ابتدا فقط داده‌های بازار عمومی و حالت `PAPER` را پیاده‌سازی کنید.
3. تست‌های واحد، بک‌تست با کارمزد و slippage، و paper trading پیوسته را اجرا کنید.
4. معامله‌ی زنده فقط با اقدام آگاهانه‌ی مالک سیستم و تنظیمات صریح فعال شود.
5. کلیدها را در متغیرهای محیطی یا secret manager نگه دارید؛ هرگز در Git یا فایل YAML قرار ندهید.

## پیاده‌سازی فعلی (حالت PAPER)
کد در `src/cat/` است و نقطه‌ی ورود `trader.py`:

| ماژول | نقش |
|---|---|
| `data.py` | داده‌ی عمومی Gate.io + cross-check با MEXC، اعتبارسنجی کندل، تازگی داده، اسلیپیج و دقت/حداقل نماد |
| `indicators.py` | SMA/EMA/RSI/ATR، نوسان محقق‌شده، شیب، تشخیص regime |
| `strategy.py` | تحلیل و `Proposal` (فقط سیگنال؛ تصمیم نهایی نیست) |
| `risk.py` | Risk Gate قطعی: اندازه، زیان روز/هفته، drawdown، cooldown، بودجه‌ی خطا، spread، kill switch |
| `broker.py` | پیپر بروکر + `LiveExecutor` که همیشه رد می‌کند (هیچ adapter زنده وصل نیست) |
| `engine.py` | چرخه: settle → snapshot → proposal → risk → order → fill → audit → persist |
| `backtest.py` | بک‌تست رویدادمحور، اجرا روی کندل بعدی، stop-first، بدون look-ahead |
| `audit.py` | لاگ ضمیمه‌پذیر با زنجیره‌ی hash و حذف اسرار |
| `report.py` | گزارش روزانه‌ی فارسی مطابق `prompts/DAILY_REPORT_TEMPLATE_FA.md` |

### اجرا
```bash
cp config.example.yaml config.yaml     # پیش‌فرض: mode=PAPER, live_trading_enabled=false
python trader.py check                  # داده، ساعت، filters، kill switch
python trader.py cycle                  # یک چرخه‌ی کامل (بدون سفارش در dry-run: --dry-run)
python trader.py loop --cycles 6        # پیاپی
python trader.py status                 # equity، موقعیت‌ها، سفارش‌ها، خطاهای پیاپی
python trader.py report                 # گزارش روزانه در state/reports/
python trader.py backtest --days 365    # بک‌تست روی داده‌ی واقعی
python tools/gate_a_backtest.py         # جدول شواهد دروازه‌ی A (چند پنجره)
python trader.py verify-audit           # یکپارچگی زنجیره‌ی audit
python trader.py stop                   # kill switch؛ resume برای برداشتن آن
python trader.py serve                  # API + داشبورد روی 127.0.0.1:8177
python tools/build_universe.py          # بازتولید فهرست ۱۰۰ جفت USDT (CoinGecko + Gate) در universe.json و config.yaml
pytest -q                               # تست‌های آفلاین ایمنی و مهندسی
```
`python trader.py enable-live --owner NAME --capital N` تنها راه ساخت رکورد تأیید مالک است و فقط با تصمیم آگاهانه‌ی خود مالک اجرا می‌شود؛ عامل هرگز آن را اجرا نمی‌کند و سقف‌ها را بالا نمی‌برد.

داده از طریق پروکسی محلی در `config.yaml → data.proxy` گرفته می‌شود؛ بدون پروکسی، `check` با fail-closed هیچ سفارشی نمی‌فرستد.

### جهان معاملاتی و نمودار زنده
`config.yaml` به‌صورت پیش‌فرض ۱۰۰ جفت USDT برتر Gate را بر اساس ارزش بازار (snapshot در `universe.json` با digest و منبع) نگه می‌دارد. کارت «بازار» در داشبورد همین فهرست را از `GET /api/v1/universe` می‌خواند و کندل‌ها را زنده از `GET /api/v1/market/candles?symbol=SOL_USDT&timeframe=4h&limit=180` می‌گیرد؛ پاسخ شامل `provenance` (منبع `gate:SYM:tf`، پنجره، زمان دریافت، کش) است. نماد خارج از فهرست → 404، timeframe نامعتبر → 400، قطعی صرافی → 503 (fail-closed). اگر می‌خواهید فهرست کوچک‌تر بماند، همان `symbols` را در `config.yaml` دستی محدود کنید — API و نمودار همان را دنبال می‌کنند.

### وضعیت دروازه‌ها
- دروازه‌ی A (داده و بک‌تست): اجرا شده — `state/reports/gate_a_backtest_2026-10-23.md`. بازده خالص در هشت پنجره بین `−0.97%` و `+0.00%` است، profit factor در بیشتر پنجره‌ها زیر ۱، و بیشترین drawdown زیر ۱.۳٪. این شواهد مجوز LIVE را نمی‌دهد.
- دروازه‌ی B (paper): در حال اجرا — سفارش‌ها فقط `paper-` هستند، وضعیت‌ها persist می‌شوند و پس از restart بازیابی می‌شوند.
- دروازه‌ی C (امنیت): بدون کلید در مخزن، audit بدون اسرار، تأیید مالک به digest پیکربندی گره‌خورده.
- دروازه‌ی D (live): خاموش. سه شرط مستقل لازم است: `mode: LIVE`، `live_trading_enabled: true`، و فایل تأیید مالک در `state/owner_confirmation.json`؛ حتی آن‌گاه `LiveExecutor` هیچ adapter زنده‌ای ندارد و سفارش نمی‌فرستد.

این بسته توصیه‌ی سرمایه‌گذاری یا وعده‌ی بازدهی نیست و جایگزین بررسی مقررات کشور محل اقامت و شرایط صرافی نمی‌شود.
