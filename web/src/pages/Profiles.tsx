import ProfileBoard from "../components/ProfileBoard";

/**
 * `/profiles` — the development ladder: each run keeps every stage's measured
 * profit, loss, fees and holding time so "stage 1 earned X, stage 2 earned Y"
 * is a recorded fact rather than a remembered claim.
 */
export default function Profiles() {
  return (
    <ProfileBoard
      market="crypto"
      description={
        <>
          این صفحه اجراهای ثبت‌شده‌ی بک‌تست را مرحله‌به‌مرحله نشان می‌دهد: هر مرحله فقط یک تغییر نسبت به مرحله‌ی قبل دارد و
          سود ناخالص، زیان ناخالص، کارمزد، خالص، نرخ برد و مدت نگه‌داشت همان مرحله اندازه‌گیری شده است. انتخاب نامزد فقط
          روی نیمه‌ی آموزش انجام می‌شود و نیمه‌ی اعتبارسنجی فقط داوری می‌کند.
        </>
      }
    />
  );
}
