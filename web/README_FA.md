# کنسول وب پژوهش ( `web/` )

رابط کاربری فارسی و راست‌به‌چپ برای پلتفرم پژوهش و حساب کاغذی رمزارز. ساخته‌شده با
Vite + React + TypeScript، بدون کتابخانه‌ی نمودار و بدون UI kit؛ تمام توکن‌های رنگ،
فاصله و جدول‌ها در `src/theme.css` دست‌نویس شده‌اند.

## پیش‌نیاز

- Node.js ۲۰ یا بالاتر (تست‌شده با v24.13.0) و npm ۱۱
- بک‌اند محلی روی `http://127.0.0.1:8177` با مسیر پایه‌ی `/api/v1`
  (قرارداد: [`docs/API_CONTRACT_FA.md`](../docs/API_CONTRACT_FA.md))

## نصب

```bash
cd web
npm install
```

به شبکه‌ی داخلی نیاز نیست؛ `npm install` با رجیستری پیش‌فرض این ماشین بدون تنظیم
متغیر محیطی پروکسی موفق بود (۷۰ بسته). اگر در شبکه‌ی دیگری رجیستری مستقیم نشد،
در همین پوشه یک `.npmrc` بگذارید:

```ini
proxy=http://127.0.0.1:1080
https-proxy=http://127.0.0.1:1080
```

## اجرای توسعه

```bash
npm run dev
```

- آدرس: `http://localhost:5173`
- `vite.config.ts` مسیر `/api` را به `http://127.0.0.1:8177` پروکسی می‌کند، پس نیازی
  به تنظیم `VITE_API_BASE` نیست و CORS درگیر نمی‌شود.
- اگر بک‌اند روی_HOST_/پورت دیگری است، `VITE_API_BASE` را در `.env.local` بگذارید
  (مثلاً `VITE_API_BASE=http://127.0.0.1:8177`). در این حالت درخواست‌ها مستقیم
  می‌روند و بک‌اند باید `CORS` را برای `http://localhost:5173` باز کند.

## ساخت و پیش‌نمایش

```bash
npm run build      # tsc --noEmit + vite build  → dist/
npm run preview    # serv کردن dist روی پورت محلی
npm run typecheck  # فقط بررسی تایپ‌ها
```

## قوانینی که در کد enforce شده‌اند

- **هیچ کنترل «شروع معامله‌ی زنده» در این UI وجود ندارد — عمداً.** تنها تغییر
  وضعیت اجرا که از رابط Possible است، کلید توقف اضطراری (`POST /kill-switch`) است؛
  نه فعال‌کردن live، نه افزایش بودجه، نه تغییر سقف زیان. حالت اجرا فقط از
  `/health` خوانده و نمایش داده می‌شود.
- **هیچ عدد نمونه‌ای نمایش داده نمی‌شود.** هر فیلد عددی در تایپ‌های `src/api/client.ts`
  nullable است و هر مقدار غایب با عبارت `داده‌ای موجود نیست` (`NO_DATA` در
  `src/format.ts`) رندر می‌شود — نه صفر، نه خط تیره‌ی مبهم.
- **منشأ داده جدا قابل‌تماشاست.** بَج‌های `تاریخچه` / `شبیه‌سازی` / `پیپر` / `زنده`
  از فیلد `provenance.kind` می‌آیند؛ نتیجه‌ی بک‌تست شبیه‌سازی و نتیجه‌ی پیپر هرگز
  یک‌شکل نیستند. نمودارها بدون داده، محورهای ساختگی نمی‌کشند.
- **دستورالعمل ثابت:** این گزارش سابقه‌ی ثبت‌شده است، نه پیش‌بینی؛ بازده گذشته
  تضمین‌کننده‌ی آینده نیست و ممکن است تمام سرمایه‌ی اختصاص‌یافته از دست برود.
- اعداد با رقم‌های لاتین و جداکننده‌ی هزارگان فارسی قالب‌بندی می‌شوند
  (`toLocaleString("fa-IR-u-nu-latn")`).
- دسترسی‌پذیری: `dir="rtl"` روی `<html>`، ناوبری کامل با کیبورد، `focus-visible`
  مشخص، نواحی وضعیت با `aria-live`، `prefers-reduced-motion` رعایت می‌شود، و فونت
  وب از CDN گرفته نمی‌شود (زنجیره‌ی Vazirmatn/Tahoma محلی).

## ساختار

```
src/
  api/client.ts        لایه‌ی fetch تایپ‌شده + useAsync + unwrap + توابع endpoint
  theme.css            توکن‌ها و استایل‌های دست‌نویس (dark-first + light)
  format.ts            قالب‌بندی فارسی اعداد/تاریخ + ثابت NO_DATA
  nav.ts               گروه‌های منو + متن هشدار ثابت محصول
  components/          Stat, Badge, Table, Filters, Modal, StateBlocks, ProvenanceNote
  charts/              CandleChart, LineChart, DrawdownChart, MetricBars (SVG خالص)
  pages/               Dashboard و PerformanceReports کامل؛ ۱۱ صفحه‌ی دیگر آماده‌ی پرکردن
```

مسیرها: `/` داشبورد، `/data`، `/analysis`، `/strategies`، `/backtests`، `/paper`،
`/trades`، `/portfolio`، `/risk`، `/reports`، `/versions`، `/logs`، `/settings`.

## شکاف قرارداد که برای نمودار کندل لازم است

`GET /datasets/{id}/bars?limit=&from=&to=` (پاسخ: `BarsPage` با `bars[]` و `overlays[]`)
در `docs/API_CONTRACT_FA.md` تعریف نشده است. تا وقتی این endpoint ساخته نشود،
`CandleChart` پیام `داده‌ای موجود نیست` نشان می‌دهد و هیچ سری ساختگی رسم نمی‌کند.
همچنین `/performance` پارامتر `timeframe` ندارد، پس تفکیک تایم‌فریم در داشبورد از
ردیف‌های واقعی `/trades` دسته‌بندی می‌شود و همین موضوع روی صفحه اعلام شده است.
