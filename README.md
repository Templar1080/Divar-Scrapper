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

## نقشه‌ی زنده‌ی دیوار (divar-mcp) — همه‌ی آگهی‌ها + مقایسه‌ی خودکار با فروشگاه‌ها
```bash
pip install -r requirements-live.txt
python live_map_server.py                      # http://localhost:8000 — هر ۶ ساعت خودکار به‌روز می‌شود
python live_map_server.py --every-hours 3 --radius-km 8 --city "تهران:18"
python live_map_server.py --demo               # نمایش آفلاین با داده‌ی ساختگی (برای دیدن کارکرد)
python divar_live.py run                       # فقط یک بار جمع‌آوری، بدون سرور
python -m pytest -q tests/                     # تست‌های آفلاین
```
- دور هر فروشگاه (پیش‌فرض ۶ کیلومتر) با کاشی‌های ۲ کیلومتری روی **نقشه‌ی دیوار** جست‌وجو می‌شود؛
  کاشی‌ای که بیش از ~۲۰۰ آگهی دارد خودکار ۴ تکه می‌شود تا چیزی جا نماند. دسته‌ها: `shop-rent`, `office-rent`.
- آگهی‌های بدون پین از فهرست محله گرفته و روی مرکز محله با برچسب «موقعیت تقریبی» گذاشته می‌شوند.
- هر آگهی تازه یک بار باز می‌شود (ودیعه/اجاره‌ی دقیق، متراژ، طبقه، محله، مختصات)؛ اول آگهی‌های نزدیک فروشگاه‌ها.
  سقف هر اجرا `--max-details 1500` است و بقیه در اجرای بعدی. تا آن موقع قیمتِ گردشده‌ی نقشه نمایش داده می‌شود.
- خروجی در `live_data/` (در git نیست): `data.json` (همان اسکیمای `divar_scraper.py`)، `history.json`،
  `store_comparison.csv` (هر فروشگاه در برابر بازار ۳ کیلومتری).
- `real_estate_map.html` وقتی از این سرور باز شود کارت «دیوار زنده» را نشان می‌دهد، بعد از هر اجرا داده را
  خودش دوباره می‌خواند و همه‌ی فروشگاه‌ها را دوباره امتیاز می‌دهد. باز کردن فایل به‌تنهایی مثل قبل از ابرآروان می‌خواند.
- باید روی سیستمی اجرا شود که به divar.ir دسترسی دارد (یا `DIVAR_MCP_PROXY`). اگر دیوار صفحه‌ی ضدربات نشان داد:
  `pip install "divar-mcp[browser]==0.3.1" && patchright install chromium`.

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
