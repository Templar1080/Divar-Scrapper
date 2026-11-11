"""ضریب کیفیت بنگاه — لایه‌ی تعدیل شرایط خودِ مغازه.

چه چیزی از داده برمی‌آید و چه چیزی نه؟ (این تفکیک، قلبِ «دقت» است)

برآوردِ تجربیِ ممکن (از ۲۴٫۵ هزار آگهی اجاره‌ی تجاری، ۳۶۴ گروه محله‌ای زوج‌شده):
    • ضریب طبقه برای فضاهای اداری/تجاریِ بالای همکف
    • ضریب نبش بودن  ≈ ۱٫۱۳
    • ضریب پاساژ نسبت به برِ خیابان ≈ ۱٫۲۰  ← نه به این معنا که پاساژ «گران‌تر» است؛
      به این معنا که مغازه‌ی بر خیابان، ارزشش را در «سرقفلی» نگه می‌دارد نه در اجاره.

برآوردِ تجربیِ ناممکن با این داده:
    • ضریب «همکف بودن» برای مغازه، چون فقط ۵٪ آگهی‌های خرده‌فروشی طبقه‌شان را
      در فیلد ساختاریافته دارند و همان‌ها با سرقفلی مخلوط‌اند.
      → این ضریب از عرف بازار گرفته می‌شود و در داشبورد قابل ویرایش است.

قرارداد: همه‌ی ضرایب نسبت به «مغازه‌ی همکفِ بر خیابان» = ۱٫۰۰ هستند و روی
«اجاره‌ی معادلِ همکف» (eff_g) اعمال می‌شوند.
"""
from __future__ import annotations
import json, math
from pathlib import Path
import numpy as np
import pandas as pd

# ------------------------------------------------------------------ ضرایب پایه
# هر ردیف: (ضریب, منبع, توضیح)   منبع: "emp" = برآورد تجربی | "prior" = عرف بازار
FLOOR_FACTORS = {
    "ground":    (1.00, "ref",   "همکف — مرجع"),
    "f1":        (0.80, "prior", "طبقه اول بدون ویترین مستقل"),
    "f2":        (0.66, "prior", "طبقه دوم"),
    "f3":        (0.56, "prior", "طبقه سوم"),
    "f4p":       (0.48, "prior", "طبقه چهارم و بالاتر"),
    "basement":  (0.55, "emp",   "زیرزمین (برآورد اداری: ۰٫۶۸؛ با تخفیف برای مغازه)"),
    "unknown":   (1.00, "na",    "نامشخص — بدون تعدیل"),
}
POS_FACTORS = {
    "street":    (1.00, "ref",   "بر خیابان — مرجع"),
    "passage":   (0.85, "prior", "داخل پاساژ (ویترین مشترک، بدون تابلوی مستقل)"),
    "mall":      (1.00, "prior", "واحد داخل مال/مجتمع برند — پیش‌فرض خنثی؛ برآورد تجربی ۱٫۲۰ به نفع پاساژ است ولی با سرقفلی خیابان مخدوش می‌شود"),
    "alley":     (0.65, "prior", "کوچه/پس‌کوچه، دید پایین"),
    "unknown":   (1.00, "na",    "نامشخص"),
}
CORNER_FACTOR   = (1.12, "emp", "نبش / دو بر (برآورد تجربی ۱٫۱۳)")
FRONTAGE_FACTORS = {
    "narrow": (0.88, "prior", "ویترین باریک، کمتر از ۳ متر"),
    "normal": (1.00, "prior", "۳ تا ۶ متر"),
    "wide":   (1.08, "prior", "۶ تا ۱۲ متر"),
    "xwide":  (1.14, "prior", "بیش از ۱۲ متر / دو دهنه"),
}
VISIBILITY_FACTORS = {
    "hidden": (0.85, "prior", "دید کم / ورودی غیرمستقیم"),
    "normal": (1.00, "prior", "معمولی"),
    "prime":  (1.08, "prior", "دید ممتاز / جریان پیاده بالا"),
}

# سرقفلی: در بازار ایران بخشی از ارزش مغازه در سرقفلی می‌نشیند نه اجاره.
# اگر مغازه‌ی شما سرقفلی ندارد ولی رقبا دارند، «گران‌بودن اجاره» توهم است.
KEY_MONEY_NOTE = ("سرقفلی/حق کسب و پیشه در معادله‌ی اجاره دیده نمی‌شود. "
                  "برای مقایسه‌ی منصفانه باید «هزینه‌ی کامل ورود» = سرقفلی + ودیعه در نظر گرفته شود.")


def factors(overrides: dict | None = None) -> dict:
    """جدول ضرایب (با امکان بازنویسی از داشبورد)."""
    f = {
        "floor": {k: v[0] for k, v in FLOOR_FACTORS.items()},
        "position": {k: v[0] for k, v in POS_FACTORS.items()},
        "corner": CORNER_FACTOR[0],
        "frontage": {k: v[0] for k, v in FRONTAGE_FACTORS.items()},
        "visibility": {k: v[0] for k, v in VISIBILITY_FACTORS.items()},
    }
    for sec, val in (overrides or {}).items():
        if isinstance(val, dict):
            f.setdefault(sec, {}).update(val)
        else:
            f[sec] = val
    return f


def qual(attrs: dict | None, f: dict | None = None) -> float:
    """ضریب کیفیت کل (۱ = مغازه‌ی همکفِ بر خیابان با شرایط استاندارد)."""
    f = f or factors()
    a = attrs or {}
    q = 1.0
    q *= f["floor"].get(str(a.get("floor", "ground")), 1.0)
    q *= f["position"].get(str(a.get("position", "street")), 1.0)
    if a.get("corner"):
        q *= f["corner"]
    q *= f["frontage"].get(str(a.get("frontage", "normal")), 1.0)
    q *= f["visibility"].get(str(a.get("visibility", "normal")), 1.0)
    return float(q)


def meta() -> dict:
    """جدول کامل با منبع هر ضریب — برای نمایش در تب متد و ویرایش در داشبورد."""
    return {
        "floor": {k: {"v": v[0], "src": v[1], "note": v[2]} for k, v in FLOOR_FACTORS.items()},
        "position": {k: {"v": v[0], "src": v[1], "note": v[2]} for k, v in POS_FACTORS.items()},
        "corner": {"v": CORNER_FACTOR[0], "src": CORNER_FACTOR[1], "note": CORNER_FACTOR[2]},
        "frontage": {k: {"v": v[0], "src": v[1], "note": v[2]} for k, v in FRONTAGE_FACTORS.items()},
        "visibility": {k: {"v": v[0], "src": v[1], "note": v[2]} for k, v in VISIBILITY_FACTORS.items()},
        "key_money_note": KEY_MONEY_NOTE,
    }


# ------------------------------------------------------------------ برآورد تجربی
POS_KW = {
    "passage": ["پاساژ", "مجتمع تجاری", "مجتمع", "مگامال", "سنتر", "غرفه", "فودکورت"],
    "street": ["بر خیابان", "خیابان", "بلوار", "ویترین", "بر اصلی", "کف خیابان", "رو به خیابان"],
    "corner": ["نبش", "دو بر", "چهارراه", "چهار راه", "سه راه"],
}
RETAIL_KW = ["مغازه", "فروشگاه", "سوپرمارکت", "سوپر مارکت", "هایپر", "غرفه", "پاساژ",
             "ویترین", "دهنه", "دکه", "کیوسک", "میوه", "قصابی", "نانوایی", "خشکبار", "بقالی", "تجاری"]


def floor_bucket(v) -> str:
    """برچسب طبقه → سطل استاندارد (ground/f1/f2/f3/f4p/basement/unknown)."""
    import re as _re
    s = "" if v is None else str(v).strip()
    if s in ("", "nan", "None", "NaN"):
        return "unknown"
    if any(k in s for k in ("زیر", "منفی")) or s.startswith("-1"):
        return "basement"
    if any(k in s for k in ("همکف", "هم کف")) or s in ("0", "0.0"):
        return "ground"
    if "اول" in s:
        return "f1"
    if "دوم" in s:
        return "f2"
    if "سوم" in s:
        return "f3"
    m = _re.search(r"-?\d+", s)
    if not m:
        return "unknown"
    n = int(m.group(0))
    if n < 0:
        return "basement"
    return {0: "ground", 1: "f1", 2: "f2", 3: "f3"}.get(n, "f4p")


def _flags(t: str) -> dict:
    t = (t or "").replace("\u200c", " ")
    p = any(k in t for k in POS_KW["passage"])
    s = any(k in t for k in POS_KW["street"])
    c = any(k in t for k in POS_KW["corner"])
    return {"passage": p and not s, "street": s and not p, "corner": c}


def empirical(rate: float = 0.03) -> dict:
    """برآورد زوج‌شده در سطح محله (میانه‌ی نسبت‌ها) روی آگهی‌های واقعی."""
    root = Path(__file__).resolve().parent.parent
    d = pd.DataFrame(json.loads((root / "data/data.json").read_text(encoding="utf-8")))
    d = d[(d.category == "commercial") & (d.deal_type == "rent")].copy()
    d["dep"] = d.deposit.fillna(0).clip(lower=0)
    d["rent"] = d.monthly_rent.fillna(0).clip(lower=0)
    d = d[(d.dep > 0) | (d.rent > 0)]
    d = d[d.area_sqm.between(10, 2000)]
    d["k"] = (d.rent + rate * d.dep) / d.area_sqm
    d["fb"] = d.floor.apply(floor_bucket)
    fl = pd.DataFrame([_flags(x) for x in d.title.fillna("")], index=d.index)
    d = pd.concat([d, fl], axis=1)

    pairs = {"basement": [], "f1": [], "f2": [], "f3": [], "f4p": []}
    pos_pairs, corner_pairs = [], []
    for _, g in d.groupby(["city", "neighborhood"]):
        gk = g[g.fb != "unknown"]
        gr = gk[gk.fb == "ground"].k
        if len(gr) >= 3:
            for b in pairs:
                u = gk[gk.fb == b].k
                if len(u) >= 3:
                    pairs[b].append(math.log(float(np.median(u) / np.median(gr))))
        if g.street.sum() >= 3 and g.passage.sum() >= 3:
            pos_pairs.append(math.log(float(np.median(g[g.passage].k) / np.median(g[g.street].k))))
        if g.corner.sum() >= 2 and (~g.corner).sum() >= 3:
            corner_pairs.append(math.log(float(np.median(g[g.corner].k) / np.median(g[~g.corner].k))))

    out = {"floor": {}, "position": {}, "n_pairs": {}}
    for b, v in pairs.items():
        out["floor"][b] = {"mult": round(math.exp(float(np.median(v))), 3) if v else None,
                          "pairs": len(v)}
        out["n_pairs"][b] = len(v)
    out["position"]["passage_vs_street"] = {
        "mult": round(math.exp(float(np.median(pos_pairs))), 3) if pos_pairs else None,
        "pairs": len(pos_pairs)}
    out["position"]["corner"] = {
        "mult": round(math.exp(float(np.median(corner_pairs))), 3) if corner_pairs else None,
        "pairs": len(corner_pairs)}
    out["n_ads"] = int(len(d))
    out["note"] = ("ارقام «mult» نسبتِ میانه‌ی هزینه‌ی هر متر آگهی‌های همان طبقه/موقعیت به "
                   "همکفِ همان محله است. نمونه‌ی همکفِ خرده‌فروشی کم است، پس این اعداد "
                   "فقط برای فضاهای اداری/تجاری معتبرند و ضریب «همکف» از عرف بازار گرفته شده.")
    return out


def build_model() -> dict:
    emp = empirical()
    return {"factors": factors(), "meta": meta(), "empirical": emp}


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from pipeline import OUT
    m = build_model()
    p = OUT / "quality_model.json"
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(m["empirical"], ensure_ascii=False, indent=1))
    print("→", p)


if __name__ == "__main__":
    main()
