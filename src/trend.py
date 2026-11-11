"""روند اجاره/ودیعه به ازای متر + تورم — برای تصمیم سرمایه‌گذاری.

خروجی: out/trend.json

هر «عکس فوری» (snapshot) یک ماه است. با هر بار اسکرپ دوباره اجرا می‌شود و
عکس‌های ماه‌های قبل حفظ می‌شوند (append)، پس شاخص رشد به‌طور خودکار ساخته می‌شود.

معیارها (همه تومان):
    rent_psqm : میانه‌ی (اجاره + ۰٫۰۳×ودیعه) ÷ متراژ   → «برآیند متر به اجاره» (ماهانه)
    dep_psqm  : میانه‌ی ودیعه ÷ متراژ
    sale_psqm : میانه‌ی قیمت کل ÷ متراژ
    gross_y   : بازده ناخالص سالانه ≈ ۱۲×rent_psqm ÷ sale_psqm
    rahn_share: ودیعه ÷ قیمت خرید (سهم ودیعه از ارزش ملک)
"""
from __future__ import annotations
import json, math
from pathlib import Path
import numpy as np
import pandas as pd

RAHN_RATE = 0.03
OUT = Path(__file__).resolve().parent.parent / "out"
RAW = Path(__file__).resolve().parent.parent / "data/data.json"

# ------------------------------------------------------------------ تورم رسمی
# منبع: مرکز آمار ایران (گزارش‌های شاخص قیمت مصرف‌کننده و شاخص اجاره‌بها)
INFLATION = {
    "unit": "درصد",
    "points": [
        {"period": "دی ۱۴۰۴",  "rent_yoy": 32.7, "rent_annual": 35.2, "cpi_yoy": 60.0, "cpi_annual": 44.6},
        {"period": "بهمن ۱۴۰۴", "rent_yoy": 32.3, "rent_annual": 34.7, "cpi_yoy": 68.1, "cpi_annual": 47.5,
         "rent_mom": 2.2, "cpi_mom": 9.4},
        {"period": "اسفند ۱۴۰۴", "rent_yoy": 31.7, "rent_annual": 34.2},
        {"period": "مهر ۱۴۰۵",  "rent_yoy": 33.4, "rent_annual": 32.2, "cpi_yoy": 88.7,
         "gap_vs_cpi": 55.3},
    ],
    "sources": [
        "مرکز آمار ایران — گزارش شاخص قیمت مصرف‌کننده، بهمن و اسفند ۱۴۰۴ و مهر ۱۴۰۵",
    ],
    "read": ("تورم نقطه‌به‌نقطه‌ی اجاره در مهر ۱۴۰۵ حدود ۳۳٪ و تورم عمومی حدود ۸۹٪ بوده است؛ "
             "یعنی اجاره‌ی مغازه در یک سال اخیر حدود «یک‌سومِ» تورم عمومی رشد کرده. "
             "نتیجه‌ی سرمایه‌گذاری: اجاره‌ی واقعی (تعدیل‌شده با تورم) در حال کاهش است و "
             "بازده اسمی اجاره به‌تنهایی توجیه‌کننده نیست؛ ارزش در «سرقفلی/افزایش قیمت ملک» "
             "و در «قدرت چانه‌زنی روی قرارداد» است."),
}

LATEST_RENT_YOY = 33.4   # مهر ۱۴۰۵
LATEST_CPI_YOY = 88.7


def _med(s: pd.Series):
    s = s.dropna()
    return float(s.median()) if len(s) else None


def snapshot(df: pd.DataFrame, df_sale: pd.DataFrame, ym: str) -> dict:
    d = df.copy()
    d["rent_eq"] = d.rent + RAHN_RATE * d.dep
    d["area"] = d.area_sqm
    # لنگر فروشِ همان محله (اگر آگهی فروش در محله نبود، میانه‌ی شهر)
    if len(df_sale):
        nb = (df_sale.groupby(["city", "neighborhood"]).sale_psqm.median().rename("nb_sale").reset_index())
        ct = (df_sale.groupby("city").sale_psqm.median().rename("city_sale").reset_index())
        d = d.merge(nb, on=["city", "neighborhood"], how="left").merge(ct, on="city", how="left")
        d["sale_psqm"] = d.get("nb_sale").fillna(d.get("city_sale")) if "nb_sale" in d else d.get("city_sale")
    else:
        d["sale_psqm"] = np.nan
    _dd = pd.to_datetime(d.get("date_scraped"), errors="coerce")
    out = {"ym": ym, "n": int(len(d)),
           "from": _dd.min().strftime("%Y-%m-%d") if _dd.notna().any() else None,
           "to": _dd.max().strftime("%Y-%m-%d") if _dd.notna().any() else None}
    # ---- به تفکیک شهر
    by_city = []
    for city, g in d.groupby("city"):
        rent = _med(g.rent_eq / g.area)
        dep = _med(g.dep / g.area)
        sale = _med(g.sale_psqm)
        y = (12 * rent / sale) if (rent and sale) else None
        by_city.append({"city": city, "n": int(len(g)),
                        "rent_psqm": round(rent) if rent else None,
                        "dep_psqm": round(dep) if dep else None,
                        "sale_psqm": round(sale) if sale else None,
                        "gross_yield": round(y, 4) if y else None,
                        "rahn_share": round(dep / sale, 3) if (dep and sale) else None})
    by_city.sort(key=lambda r: -(r["n"] or 0))
    out["by_city"] = by_city
    # ---- به تفکیک محله (شهرهایی که حجم دارند)
    by_nb = []
    for (city, nb), g in d.groupby(["city", "neighborhood"]):
        if len(g) < 5:
            continue
        rent = _med(g.rent_eq / g.area)
        dep = _med(g.dep / g.area)
        sale = _med(g.sale_psqm)
        by_nb.append({"city": city, "nb": nb or "—", "n": int(len(g)),
                      "rent_psqm": round(rent) if rent else None,
                      "dep_psqm": round(dep) if dep else None,
                      "sale_psqm": round(sale) if sale else None,
                      "gross_yield": round(12 * rent / sale, 4) if (rent and sale) else None})
    by_nb.sort(key=lambda r: -r["n"])
    out["by_nb"] = by_nb[:500]
    out["median"] = {"rent_psqm": _med(d.rent_eq / d.area),
                     "dep_psqm": _med(d.dep / d.area),
                     "sale_psqm": _med(d.sale_psqm)}
    return out


def build() -> dict:
    df = pd.DataFrame(json.loads(RAW.read_text(encoding="utf-8")))
    for c in ["deposit", "monthly_rent", "area_sqm", "price_total"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df.category == "commercial") & df.area_sqm.between(10, 2000)].copy()
    df["dep"] = df.deposit.fillna(0).clip(lower=0)
    df["rent"] = df.monthly_rent.fillna(0).clip(lower=0)
    df["ym"] = pd.to_datetime(df.date_scraped, errors="coerce").dt.strftime("%Y-%m")
    df = df[df.ym.notna()]
    df["sale_psqm"] = np.where((df.deal_type == "sale") & df.price_total.notna(),
                               df.price_total * 1_000_000 / df.area_sqm, np.nan)
    rent_all = df[(df.dep > 0) | (df.rent > 0)].copy()
    sale_all = df[df.sale_psqm.notna() & df.sale_psqm.between(1e5, 1e10)].copy()

    snaps = []
    for ym in sorted(set(rent_all.ym)):
        snaps.append(snapshot(rent_all[rent_all.ym == ym], sale_all[sale_all.ym == ym], ym))
    snaps.sort(key=lambda s: s["ym"])

    # حفظ عکس‌های قبلی (اگر ماه جدیدی اضافه شود)
    prev_path = OUT / "trend.json"
    if prev_path.exists():
        try:
            old = json.loads(prev_path.read_text(encoding="utf-8"))
            have = {s["ym"] for s in snaps}
            snaps = [s for s in old.get("snapshots", []) if s["ym"] not in have] + snaps
            snaps.sort(key=lambda s: s["ym"])
        except Exception:
            pass

    # ---- رشد بین عکس‌ها (اگر ≥۲ ماه داشته باشیم)
    growth = []
    for a, b in zip(snaps, snaps[1:]):
        try:
            gap = (pd.Timestamp(b.get("from") or b["ym"]) - pd.Timestamp(a.get("to") or a["ym"])).days
        except Exception:
            gap = 0
        if gap < 20:      # عکس‌های نزدیک‌تر از سه هفته، «تغییر دوره‌ای» نیستند
            continue
        ga = {r["city"]: r for r in a["by_city"]}
        for r in b["by_city"]:
            p = ga.get(r["city"])
            if p and p["rent_psqm"] and r["rent_psqm"]:
                growth.append({"city": r["city"], "from": a["ym"], "to": b["ym"],
                               "rent_change": round(r["rent_psqm"] / p["rent_psqm"] - 1, 4),
                               "dep_change": round((r["dep_psqm"] or 0) / (p["dep_psqm"] or 1) - 1, 4)
                               if p["dep_psqm"] else None})
    months = [s["ym"] for s in snaps]
    spans = [(pd.Timestamp(s.get("from") or s["ym"]), pd.Timestamp(s.get("to") or s["ym"])) for s in snaps]
    covered_days = (max(t for _, t in spans) - min(f for f, _ in spans)).days if spans else 0
    latest = snaps[-1] if snaps else None

    # ---- برآورد گذشته و آینده با تورم رسمی اجاره
    backcast, forecast = [], []
    if latest:
        r_yoy = LATEST_RENT_YOY / 100
        for r in latest["by_city"][:25]:
            if not r["rent_psqm"]:
                continue
            backcast.append({"city": r["city"],
                             "rent_now": r["rent_psqm"],
                             "rent_12m_ago_est": round(r["rent_psqm"] / (1 + r_yoy)),
                             "dep_now": r["dep_psqm"],
                             "dep_12m_ago_est": round(r["dep_psqm"] / (1 + r_yoy)) if r["dep_psqm"] else None})
            for h in (6, 12):
                forecast.append({"city": r["city"], "months": h,
                                 "rent": round(r["rent_psqm"] * (1 + r_yoy) ** (h / 12)),
                                 "rent_real": round(r["rent_psqm"] * (1 + r_yoy) ** (h / 12)
                                                    / (1 + LATEST_CPI_YOY / 100) ** (h / 12))})
    return {
        "generated": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "snapshots": snaps,
        "months": months,
        "n_months": len(months),
        "growth": growth,
        "growth_available": bool(growth),
        "covered_days": covered_days,
        "inflation": INFLATION,
        "backcast": backcast,
        "forecast": forecast,
        "assumptions": {"rahn_rate": RAHN_RATE, "rent_yoy": LATEST_RENT_YOY, "cpi_yoy": LATEST_CPI_YOY,
                        "note": ("برآورد گذشته/آینده با تورم رسمی اجاره انجام شده، نه با داده‌ی "
                                 "اندازه‌گیری‌شده. با دومین عکس فوری، ستون «تغییر واقعی» خودکار پر می‌شود.")},
    }


def main():
    t = build()
    p = OUT / "trend.json"
    p.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
    print("months:", t["months"], "| cities:", len(t["snapshots"][-1]["by_city"]),
          "| neighborhoods:", len(t["snapshots"][-1]["by_nb"]))
    print("median rent/m²:", round(t["snapshots"][-1]["median"]["rent_psqm"] or 0),
          "| median deposit/m²:", round(t["snapshots"][-1]["median"]["dep_psqm"] or 0),
          "| median sale/m²:", round(t["snapshots"][-1]["median"]["sale_psqm"] or 0))
    print("→", p, p.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
