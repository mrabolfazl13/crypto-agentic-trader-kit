# Skill: امنیت و ممیزی

- کلید API در متغیر محیطی/secret manager؛ فایل `.env` در `.gitignore`.
- API key فقط مجوز خواندن و معامله؛ withdrawal خاموش و ترجیحاً IP allowlist.
- برای paper trading کلید API واقعی لازم نیست.
- secrets در exception، telemetry، prompt، screenshot و log حذف شوند.
- حداقل دسترسی، dependency pinning، اسکن آسیب‌پذیری و بازبینی مجوزهای پکیج‌ها.
- احراز هویت داشبورد و محافظت CSRF/CORS در صورت وجود UI.
- audit log تغییرناپذیر برای تغییر تنظیمات، تصمیم‌ها، سفارش‌ها و ردهای Risk Gate.
- تست سناریوهای restart، قطعی شبکه، پاسخ تکراری، clock skew، داده‌ی کهنه و اختلاف موجودی.
- هیچ‌گاه seed phrase یا کلید خصوصی کیف پول را به عامل نده.
