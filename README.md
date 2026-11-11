# Divar Retail Intelligence

پایپ‌لاین تحلیل اجاره و لوکیشن برای فروشگاه‌های زنجیره‌ای، بر پایه‌ی آگهی‌های دیوار (و اسکلت اسکرپر چندمنبعی).

## ساختار
```
src/pipeline.py        پاک‌سازی، نوع‌شناسی ملک، هم‌مقیاس‌سازی، اقتصاد فروشگاه
src/valuation2.py      موتور ارزش‌گذاری (هدانیک + شاخص کیفیت لوکیشن + انقباض بیزی)
src/validate.py        اعتبارسنجی هولد-اوت و تنظیم پارامترها
src/quality.py         ضریب کیفیت بنگاه: طبقه / بر خیابان / پاساژ / نبش / ویترین
src/units.py           واحد پول: تومان، نرمال‌سازی ریال→تومان، نگهبان ۱۰×
src/trend.py           روند اجاره و ودیعه به ازای متر + تورم رسمی + پیش‌بینی
src/sources/           اسکرپر چندمنبعی: base.py، adapters.py، run_sources.py
src/run.py             اجرای کامل → out/app_data.json + CSV/XLSX + قالب ویژگی‌ها
src/report.py          ساخت گزارش فارسی
app/template.html      رابط کاربری (RTL + three.js + نقشه کلاسیک ۲بعدی)
build_app.py           تزریق three.js و داده در HTML نهایی
```

## اجرا
```bash
cd src
python3 validate.py     # تنظیم/اعتبارسنجی مدل (~۴۰ ثانیه)
python3 quality.py      # برآورد تجربی ضرایب کیفیت → out/quality_model.json
python3 trend.py        # روند و تورم → out/trend.json
python3 run.py          # ساخت خروجی‌ها
python3 report.py       # گزارش فارسی
cd .. && python3 build_app.py   # ساخت out/divar_intel.html
```

## اسکرپر چندمنبعی (شیپور / کلید / دیوار)
```bash
cd src/sources
python3 run_sources.py --init-config                  # ساخت config_sources.json
python3 run_sources.py --source sheypoor --city tehran --pages 3
python3 run_sources.py --source all --cities tehran,isfahan --pages 2
python3 run_sources.py --fixture sample.html --source kilid --city tehran   # تست آفلاین
python3 run_sources.py --merge                        # ادغام با data.json دیوار → data_all.json
```
- هر آداپتور اول JSON امبدشده در صفحه (`__NEXT_DATA__` / `NUXT` / JSON-LD) را می‌خواند،
  بعد API و در آخر regex.
- **همه‌ی ارقام خروجی به تومان نرمال می‌شوند** (`units.to_toman`)؛ اگر منبعی ریال بدهد ÷۱۰ می‌شود.
- حذف تکراری بین منابع: مکان گردشده + متراژ + قیمت (`Listing.key`).
- نکته: دامنه‌های ایرانی از محیط اجرای ما قابل دسترسی نیستند؛ اتصال باید از سیستم خودت تست شود.
  با `--fixture` می‌توانی ساختار صفحه را آفلاین آزمایش کنی.

## ویژگی‌های فروشگاه‌ها (برای دقت مقایسه)
`python3 run.py` فایل `out/قالب_ویژگی_فروشگاه‌ها.csv` را می‌سازد. مقادیر مجاز:
```
floor      : ground | f1 | f2 | f3 | f4p | basement
position   : street | passage | mall | alley
corner     : 0 | 1
frontage   : narrow | normal | wide | xwide
visibility : hidden | normal | prime
```
همان فایل را با نام `out/ویژگی_فروشگاه‌ها.csv` ذخیره کن و `run.py` را دوباره اجرا کن.
(در داشبورد هم قابل ویرایش زنده است، ولی برای دائمی‌شدن باید در CSV بیاید.)

## ورودی‌ها
- `data/data.json` — خروجی اسکرپر دیوار (تومان)
- `data/myli_stores.csv` — فایل اکسل فروشگاه‌ها (در واقع xlsx)

## خروجی‌ها
- `out/divar_intel.html` — داشبورد تعاملی تک‌فایل
- `out/store_scores.csv`, `out/نتیجه_تحلیل_فروشگاه‌ها.xlsx`
- `out/گزارش_تحلیلی.md`, `out/validation.json`
- `out/quality_model.json`, `out/trend.json`, `out/قالب_ویژگی_فروشگاه‌ها.csv`
