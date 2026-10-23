# معماری پیشنهادی

## اجزای مستقل
1. **Market Data Service**: داده‌ی بازار از REST/WebSocket، اعتبارسنجی، timestamp و ذخیره‌سازی.
2. **Feature/Analysis Service**: محاسبه‌ی اندیکاتورها و ساخت تحلیل ساختاریافته.
3. **Strategy Agent**: پیشنهاد BUY/SELL/HOLD/NO_TRADE با دلیل و عدم‌قطعیت؛ دسترسی مستقیم به API معامله ندارد.
4. **Risk Engine (قطعی)**: اعمال سقف‌ها، محاسبه‌ی اندازه‌ی موقعیت و رد سفارش نامعتبر.
5. **Execution Service**: تنها جزء دارای مجوز ارسال سفارش؛ adapter مخصوص هر صرافی.
6. **Portfolio/Reconciliation**: تطبیق موجودی، معاملات، سفارش‌های باز و PnL.
7. **Audit & Monitoring**: رویدادهای append-only، هشدار و kill switch.
8. **Dashboard/CLI**: نمایش وضعیت و امکان توقف؛ تنظیمات حساس نیازمند احراز هویت.

## جریان داده
Market Data -> Validation -> Features -> Strategy Proposal -> Risk Engine -> Execution Service -> Exchange
Exchange -> Reconciliation -> Portfolio/Audit/Monitoring

## مرز امنیتی
- Strategy Agent نباید secret صرافی را ببیند.
- Risk Engine و Execution Service باید سیاست‌ها را مستقل از پاسخ مدل enforce کنند.
- هر سفارش باید `decision_id`, `client_order_id`, timestamp و علت داشته باشد.
- LIVE فقط وقتی مجاز است که هم `mode=LIVE` و هم `live_trading_enabled=true` و هم تأیید صریح ثبت‌شده‌ی مالک وجود داشته باشد.
- در غیر این صورت adapter باید به paper broker وصل شود.
