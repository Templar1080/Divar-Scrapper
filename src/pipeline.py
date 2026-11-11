"""
Divar Retail Intelligence — analytics pipeline
==============================================
Input : data.json (divar scraper export) + myli_stores.xlsx (store master)
Output: out/app_data.json, out/store_scores.csv, out/comps_sample.csv

متدولوژی (خلاصه):
1) پاک‌سازی: تبدیل آگهی‌های رهن/اجاره به «هزینه ماهانه مؤثر» = اجاره + ودیعه × نرخ رهن
2) نوع‌شناسی: تشخیص زیرگروه ملک از عنوان (مغازه/پاساژ/مال ↔ اداری/انبار/مسکونی)
3) نرمال‌سازی مقیاس: هزینه = k × متراژ^γ  (γ از رگرسیون لگاریتمی روی خود بازار)
4) نرمال‌سازی طبقه/نوع: تبدیل به معادل همکف خیابانی
5) ارزش منصفانه: میانه وزنی (robust) روی k آگهی‌های قابل‌مقایسه با کرنل مکانی/متراژی
6) امتیازدهی: ۵ مؤلفه + بازه اطمینان بر پایه Kish effective N و پراکندگی
"""
from __future__ import annotations
import json, math, re, sys
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_DIVAR = Path("/home/user/google_drive/data.json")
RAW_STORES = Path("/home/user/google_drive/myli_stores.csv")
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)

# ------------------------------------------------------------------ assumptions
CFG = dict(
    rahn_rate=0.03,          # نرخ تبدیل ماهانه ودیعه به اجاره (۳٪ ≈ ۳۶٪ سالانه)
    max_radius_km=5.0,       # شعاع حداکثر جست‌وجوی مشابه
    band_km=2.0,             # باند کرنل مکانی پایه
    area_log_sd=0.35,        # پهنای کرنل تشابه متراژ (در لگاریتم)
    gps_sigma_km=0.45,       # خطای موقعیت آگهی‌های تخمینی (jitter اسکرپر)
    min_comps=8,             # حداقل مشابه برای امتیاز
    winsor=(0.02, 0.98),
    weights=dict(price=0.35, sales_burden=0.25, deposit=0.15, contract=0.10, location=0.15),
)

# ------------------------------------------------------------------ text helpers
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

RETAIL_STRONG = ["مغازه", "فروشگاه", "سوپرمارکت", "سوپر مارکت", "هایپر", "هایپرمارکت",
                 "غرفه", "پاساژ", "مجتمع تجاری", "دهنه", "دکه", "کیوسک", "ویترین",
                 "میوه فروشی", "قصابی", "نانوایی", "خشکبار", "پروتئین", "لبنیات", "بقالی",
                 "سوپر مارکتی", "سنتر", "پاساژ"]
RETAIL_WEAK = ["تجاری", "بر اصلی", "کسب و کار", "کاسبی", "کاربری تجاری"]
MALL_RE = re.compile(r"(?<!ش)مال(?![ی\u200c])|پاساژ|مجتمع تجاری|سنتر|سیتی سنتر")
FOODSERV_KW = ["رستوران", "کافه", "قهوه", "فست فود", "بیرون بر", "آبمیوه", "کترینگ", "تالار"]
OFFICE_KW = ["اداری", "دفتر", "اتاق کار", "مطب", "کلینیک", "شرکت", "کارمند", "هوم افیس", "برج", "ساختمان پزشکان"]
INDUSTRIAL_KW = ["انبار", "انباری", "کارگاه", "سوله", "کارخانه", "صنعتی", "پارکینگ", "دپو"]
RESIDENTIAL_KW = ["آپارتمان", "اپارتمان", "خواب", "خانه", "مسکونی", "زمین", "ویلا",
                  "سوئیت", "پنت هاوس", "کلید نخورده", "خوش نقشه", "رهن کامل",
                  "اجاره مسکن", "بالکن", "تراس", "حیاط اختصاصی"]
SERVICE_KW = ["آرایشگاه", "سالن زیبایی", "باشگاه", "ورزشگاه", "آموزشگاه", "گالری"]

SERVICE_KW = ["آرایشگاه", "سالن زیبایی", "باشگاه", "ورزشگاه", "آموزشگاه", "گالری"]


def classify_type(title: str, category: str) -> str:
    """نوع ملک از عنوان. قاعده: نشانه‌ی صریح مغازه > اداری/مطب > بقیه."""
    t = (title or "").translate(FA_DIGITS).replace("\u200c", " ")
    strong = any(k in t for k in RETAIL_STRONG)
    weak = any(k in t for k in RETAIL_WEAK)
    mall = bool(MALL_RE.search(t))
    off = any(k in t for k in OFFICE_KW)
    ind = any(k in t for k in INDUSTRIAL_KW)
    fs = any(k in t for k in FOODSERV_KW)
    res = sum(1 for k in RESIDENTIAL_KW if k in t)
    if ind and not strong:
        return "industrial"
    if off and not strong:          # مطب/اداری حتی اگر «بر اصلی» باشد → اداری
        return "office"
    if strong or (weak and not off and not ind):
        return "retail_mall" if (mall and not strong) else "retail"
    if fs:
        return "food_service"
    if res >= 1:
        return "residential"
    return "commercial_other" if category == "commercial" else "residential"


def floor_factor(floor) -> float:
    """ضریب اجاره نسبت به همکف (۱ = همکف). برای معادل‌سازی آگهی روی فروشگاه همکف."""
    s = "" if floor is None else str(floor).translate(FA_DIGITS).strip().lower()
    if s in ("", "nan", "none"):
        return 1.0                      # نامعلوم → بدون تعدیل (وزن کمتر در مدل)
    if re.search(r"زیر|منفی|basement|-1|sub", s):
        return 0.55
    if re.search(r"هم\s*کف|ground|gf|^0$", s):
        return 1.0
    m = re.search(r"(-?\d+)", s)
    n = int(m.group(1)) if m else None
    if n is None:
        if "اول" in s: return 0.80
        if "دوم" in s: return 0.66
        if "سوم" in s: return 0.56
        return 1.0
    if n <= 0: return 0.55 if n < 0 else 1.0
    if n == 1: return 0.80
    if n == 2: return 0.66
    if n == 3: return 0.56
    return 0.48


def fa_num(v):
    if v is None: return np.nan
    if isinstance(v, (int, float, np.floating)) and not (isinstance(v, float) and math.isnan(v)):
        return float(v)
    s = str(v).translate(FA_DIGITS).replace(",", "").replace("٬", "").strip()
    if s in ("", "-", "nan", "None", " -   "): return np.nan
    try: return float(s)
    except Exception: return np.nan


# ------------------------------------------------------------------ geo
R_EARTH = 6371.0088
def haversine(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * R_EARTH * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def wquantile(x, w, q):
    """quantile وزنی"""
    x = np.asarray(x, float); w = np.asarray(w, float)
    m = np.isfinite(x) & np.isfinite(w) & (w > 0)
    if m.sum() == 0: return np.nan
    x, w = x[m], w[m]
    o = np.argsort(x); x, w = x[o], w[o]
    cw = np.cumsum(w) - 0.5 * w
    cw /= w.sum()
    return float(np.interp(q, cw, x))


def wmedian_mad(x, w):
    med = wquantile(x, w, 0.5)
    if not np.isfinite(med): return np.nan, np.nan
    mad = wquantile(np.abs(np.asarray(x, float) - med), w, 0.5)
    return med, 1.4826 * mad


# ------------------------------------------------------------------ load
def load_stores() -> pd.DataFrame:
    s = pd.read_excel(RAW_STORES)
    s.columns = [c.strip() for c in s.columns]
    s["lat"] = np.nan; s["lng"] = np.nan
    for i, c in s["coordinates"].items():
        try:
            a, b = str(c).split(",")
            s.loc[i, "lat"], s.loc[i, "lng"] = float(a), float(b)
        except Exception:
            pass
    num_cols = ["area_sqm", "monthly_rent_toman", "deposit_toman", "minimum_rent_toman",
                "sales_rent_pct", "avg_monthly_sales_toman", "avg_annual_sales_toman", "annual_increase_pct"]
    for c in num_cols:
        s[c] = s[c].apply(fa_num)
    s["sales_rent_pct"] = s["sales_rent_pct"].apply(lambda v: v / 100 if (pd.notna(v) and v > 1) else v)
    s["store_id"] = ["S%02d" % (i + 1) for i in range(len(s))]
    return s


def clean_listings() -> pd.DataFrame:
    df = pd.read_json(RAW_DIVAR)
    df = df[df.deal_type == "rent"].copy()
    for c in ["deposit", "monthly_rent", "area_sqm", "lat", "lng"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # هزینه ماهانه مؤثر (ودیعه → اجاره)
    df["dep"] = df["deposit"].fillna(0).clip(lower=0)
    df["rent"] = df["monthly_rent"].fillna(0).clip(lower=0)
    df = df[(df.dep > 0) | (df.rent > 0)]
    df = df[df.area_sqm.between(8, 3000)]
    df = df[df.lat.between(24, 40) & df.lng.between(43, 64)]
    df["eff"] = df.rent + df.dep * CFG["rahn_rate"]
    df = df[df.eff > 0]
    df["type"] = [classify_type(t, c) for t, c in zip(df.title.fillna(""), df.category.fillna(""))]
    df["mall"] = df.title.fillna("").str.contains(MALL_RE)
    df["ff"] = df.floor.apply(floor_factor)
    df["ground_known"] = df.floor.notna() & (df.floor.astype(str).str.strip() != "")
    df["eff_g"] = df.eff / df.ff                    # معادل همکف
    df["k_raw"] = df.eff_g / df.area_sqm            # هزینه/متر (همکف)
    df["exact"] = df.has_exact_location.fillna(0).astype(int)
    # --- پاک‌سازی مقادیر غیرواقعی
    # رهن کامل بی‌معنا (ودیعه‌ی خیلی کوچک بدون اجاره) یا اجاره‌ی صفر با ودیعه‌ی ناچیز
    junk = (df.rent <= 0) & (df.dep < 50e6)
    # وجود ودیعه‌ی ناچیز ولی اجاره‌ی صفر / مقادیر تایپی
    junk |= (df.dep > 0) & (df.dep < 1e6)
    df = df[~junk]
    # winsorise سهمتر در هر شهر-نوع (۱٪ تا ۹۹٪)
    grp = df.groupby(["city", "type"])["k_raw"]
    lo = grp.transform(lambda x: x.quantile(0.02) if len(x) >= 20 else np.nan)
    hi = grp.transform(lambda x: x.quantile(0.98) if len(x) >= 20 else np.nan)
    df = df[(lo.isna()) | ((df.k_raw >= lo * 0.6) & (df.k_raw <= hi * 1.6))]
    return df.reset_index(drop=True)


# ------------------------------------------------------------------ elasticity γ
def estimate_gamma(df: pd.DataFrame) -> tuple[float, dict]:
    """log(k) = a + (γ-1)·log(area) + city FE + type FE  →  γ = 1 + slope"""
    d = df[df.type.isin(["retail", "retail_mall"])].copy()
    d = d[d.area_sqm.between(15, 1200)]
    d["y"] = np.log(d.k_raw)
    d = d[np.isfinite(d.y)]
    # winsorise per city
    lo, hi = CFG["winsor"]
    for city, g in d.groupby("city"):
        if len(g) < 30:
            continue
        q1, q2 = g.y.quantile(lo), g.y.quantile(hi)
        d.loc[g.index, "y"] = g.y.clip(q1, q2)
    d = d[np.isfinite(d.y)]
    X = pd.get_dummies(d.city, drop_first=True).astype(float)
    X.insert(0, "logarea", np.log(d.area_sqm))
    X.insert(0, "const", 1.0)
    X["mall"] = (d.type == "retail_mall").astype(float)
    A = X.values; y = d.y.values
    # Huber-IRLS
    beta = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(8):
        r = y - A @ beta
        s = 1.4826 * np.median(np.abs(r - np.median(r))) + 1e-9
        u = np.clip(np.abs(r) / (1.5 * s), None, None)
        w = np.where(u <= 1, 1.0, 1.0 / u)
        beta = np.linalg.lstsq(A * w[:, None], y * w, rcond=None)[0]
    gamma = 1.0 + float(beta[list(X.columns).index("logarea")])
    fit = float(np.exp(np.median(np.abs(y - A @ beta))))
    stats = dict(gamma=round(gamma, 4), n=int(len(d)), median_abs_log_err=round(fit, 4))
    return gamma, stats


# ------------------------------------------------------------------ fair value engine
def comps_for(store, pool: pd.DataFrame, cfg=CFG):
    """مشابه‌های قابل‌مقایسه + وزن‌ها برای یک فروشگاه"""
    d = pool[pool.city == store.city] if (pool.city == store.city).any() else pool
    dist = haversine(store.lat, store.lng, d.lat.values, d.lng.values)
    d = d.assign(dist=dist)
    d = d[(d.dist <= cfg["max_radius_km"]) & d.type.isin(["retail", "retail_mall"])]
    if len(d) == 0: return d, None
    # کرنل‌ها
    sig = np.where(d.exact.values == 1, 0.0, cfg["gps_sigma_km"])
    d_eff = np.sqrt(d.dist.values ** 2 + sig ** 2)
    w_geo = np.exp(-0.5 * (d_eff / cfg["band_km"]) ** 2)
    lr = np.log(d.area_sqm.values / store.area_sqm)
    w_area = np.exp(-0.5 * (lr / cfg["area_log_sd"]) ** 2)
    w_floor = np.where(d.ground_known.values, 1.0, 0.75)
    w_type = np.where(d.type.values == "retail", 1.0, 0.9)
    w = w_geo * w_area * w_floor * w_type
    d = d.assign(w=w, d_eff=d_eff)
    d = d[d.w > 1e-4]
    return d, d.w.values if len(d) else None


def value_store(store, pool, gamma, cfg=CFG):
    d, w = comps_for(store, pool, cfg)
    res = dict(n_comps=int(len(d)))
    if len(d) < cfg["min_comps"]:
        res.update(ok=False, reason=f"تنها {len(d)} آگهی مشابه در {cfg['max_radius_km']} کیلومتری")
        return res, None
    # k معادل: هزینه آگهی برای متراژ فروشگاه  →  k = eff_g / area^γ
    k = d.eff_g.values / np.power(d.area_sqm.values, gamma)
    lk = np.log(k)
    med, mad = wmedian_mad(lk, w)
    sigma = float(mad) if np.isfinite(mad) else 0.35
    effn = float(w.sum() ** 2 / (w ** 2).sum())
    fair_cost = math.exp(med) * store.area_sqm ** gamma
    p25 = wquantile(k, w, 0.25); p75 = wquantile(k, w, 0.75)
    exact_share = float(d.exact.mean())
    res.update(ok=True, effN=round(effn, 1), sigma_log=round(sigma, 3),
               fair_cost=fair_cost, fair_psqm=fair_cost / store.area_sqm,
               band_lo=p25 * store.area_sqm ** gamma, band_hi=p75 * store.area_sqm ** gamma,
               median_dist_km=float(np.median(d.dist)), exact_share=round(exact_share, 2),
               median_psqm_comp=float(wquantile(d.k_raw.values, w, 0.5)))
    # بازه اطمینان: خطای استاندارد میانه وزنی ~ 1.253·σ/√effN
    se_log = 1.2533 * sigma / math.sqrt(max(effn, 1))
    res["ci_log"] = se_log
    res["ci_lo"] = fair_cost * math.exp(-1.96 * se_log)
    res["ci_hi"] = fair_cost * math.exp(+1.96 * se_log)
    # سهم رقابت (سوپرمارکت/میوه/هایپر در ۲ کیلومتری)
    near = d[d.dist <= 2.0]
    res["competitors"] = int(near.title.fillna("").str.contains("|".join(["سوپرمارکت", "سوپر مارکت", "هایپر", "میوه", "مواد غذایی", "بقالی"])).sum())
    return res, d


# ------------------------------------------------------------------ store economics
def _dstr(v):
    """تاریخ → رشته‌ی ISO؛ NaT/خالی → None"""
    try:
        t = pd.Timestamp(v)
        if pd.isna(t):
            return None
        return t.strftime("%Y-%m-%d")
    except Exception:
        return None


def store_economics(s: pd.Series, cfg=CFG):
    dep = 0.0 if pd.isna(s.deposit_toman) else float(s.deposit_toman)
    cash = 0.0 if pd.isna(s.monthly_rent_toman) else float(s.monthly_rent_toman)
    minr = 0.0 if pd.isna(s.minimum_rent_toman) else float(s.minimum_rent_toman)
    pct = None if pd.isna(s.sales_rent_pct) else float(s.sales_rent_pct)
    sales = None if pd.isna(s.avg_monthly_sales_toman) else float(s.avg_monthly_sales_toman)
    sales_pct_based = sales * pct if (sales and pct) else None
    structure = str(s.deal_structure or "").strip()
    flags = []
    if structure == "percent_of_sales":
        base = sales_pct_based if sales_pct_based is not None else cash
    elif structure == "hybrid":
        cand = [x for x in (sales_pct_based, minr if minr > 0 else None, cash if cash > 0 else None) if x]
        base = max(cand) if cand else 0.0
        if sales_pct_based and cash and abs(cash - sales_pct_based) / max(sales_pct_based, 1) > 0.4:
            flags.append("اجاره ثبت‌شده با درصد فروش نمی‌خواند (اختلاف >۴۰٪)")
    else:  # fixed
        base = cash
    if sales_pct_based and base and abs(base - sales_pct_based) / max(sales_pct_based, 1) > 0.6:
        flags.append("مبلغ اجاره با فروش×درصد هم‌خوانی ندارد — نیازمند بازبینی")
    eff = base + dep * cfg["rahn_rate"]
    rr = (eff / sales) if sales else None
    ann = None if pd.isna(s.avg_annual_sales_toman) else float(s.avg_annual_sales_toman)
    if sales and ann and ann < sales * 6:
        flags.append("فروش سالانه ثبت‌شده با فروش ماهانه سازگار نیست")
    return dict(structure=structure, cash_rent=base, deposit=dep, deposit_cost=dep * cfg["rahn_rate"],
                effective=eff, sales=sales, pct=pct, sales_pct_based=sales_pct_based,
                rent_to_sales=rr, min_rent=minr, annual_sales=ann,
                inc=float(s.annual_increase_pct) if pd.notna(s.annual_increase_pct) else None,
                lease_start=_dstr(getattr(s, "lease_start", "")),
                renewal=_dstr(getattr(s, "next_renewal", "")), flags=flags)


# ------------------------------------------------------------------ scoring
def score_price(gap):
    """۱۰۰ = ارزان‌تر از بازار؛ ۵۰ = دقیقاً بازار"""
    if gap is None or not np.isfinite(gap): return 50.0
    g = gap * 100  # درصد
    pts = [(-40, 100), (-25, 96), (-15, 90), (-8, 84), (0, 74), (8, 62),
           (15, 50), (25, 36), (40, 22), (60, 10), (100, 3)]
    xs, ys = zip(*pts)
    return float(np.interp(g, xs, ys))


def score_sales_burden(rt, internal_med):
    """نسبت اجاره به فروش — معیار طلایی خرده‌فروشی. مبنای مقایسه: میانه پرتفوی خودت."""
    if rt is None or not internal_med: return 50.0
    rel = rt / internal_med
    pts = [(0.5, 100), (0.8, 88), (1.0, 72), (1.2, 58), (1.5, 42), (2.0, 24), (3.0, 8)]
    xs, ys = zip(*pts)
    return float(np.interp(rel, xs, ys))


def score_deposit(dep, eff, market_dep_months):
    """ودیعه: هم بار نقدینگی، هم تناسب با عرف بازار"""
    if not dep or not eff: return 60.0
    months = dep / max(eff, 1)
    ref = market_dep_months or 6.0
    rel = months / ref
    pts = [(0, 100), (0.6, 92), (1.0, 78), (1.5, 62), (2.2, 44), (3.5, 24)]
    xs, ys = zip(*pts)
    return float(np.interp(rel, xs, ys))


def score_contract(econ, today=pd.Timestamp("2026-11-01")):
    try:
        ren = pd.Timestamp(econ["renewal"])
    except Exception:
        ren = None
    months_left = (ren - today).days / 30.44 if (ren is not None and pd.notna(ren)) else None
    inc = econ.get("inc")
    s = 60.0
    if months_left is not None:
        if months_left < 0:   s = 30 + 0
        elif months_left < 6: s = 45
        elif months_left < 12: s = 60
        elif months_left < 36: s = 75
        else: s = 85
    if inc is None:
        s -= 5
    elif inc <= 15: s += 10
    elif inc <= 25: s += 4
    elif inc <= 35: s -= 4
    else: s -= 12
    return float(np.clip(s, 0, 100)), months_left


def score_location(val, city_pool_depth):
    """عمق بازار/نقدشوندگی لوکیشن: چگالی آگهی + تنوع، نرمال‌شده با نمونه‌گیری شهر"""
    if not val.get("ok"): return 50.0, {}
    dens = val["n_comps"] / max(city_pool_depth, 1)
    s_dens = float(np.interp(math.log1p(dens * 100), [0, 1.5, 3, 4.5], [40, 60, 78, 90]))
    s_exact = 50 + 40 * val.get("exact_share", 0)
    comp = val.get("competitors", 0)
    s_comp = float(np.interp(comp, [0, 3, 8, 15, 30], [55, 75, 78, 62, 45]))
    total = 0.45 * s_dens + 0.25 * s_exact + 0.30 * s_comp
    return float(np.clip(total, 0, 100)), dict(density_score=s_dens, exact_score=s_exact, comp_score=s_comp, competitors=comp)


def confidence_of(val):
    if not val.get("ok"): return 0
    effn = val.get("effN", 0); sigma = val.get("sigma_log", 0.5)
    c_n = float(np.interp(effn, [3, 6, 10, 18, 30, 60], [20, 45, 62, 78, 88, 96]))
    c_s = float(np.interp(sigma, [0.15, 0.3, 0.45, 0.6, 0.85], [100, 88, 72, 55, 35]))
    c_e = 55 + 45 * val.get("exact_share", 0)
    return float(np.clip(0.45 * c_n + 0.35 * c_s + 0.20 * c_e, 0, 100))


def grade(total):
    if total >= 82: return "A"
    if total >= 68: return "B"
    if total >= 54: return "C"
    return "D"


def clean_sales() -> pd.DataFrame:
    """آگهی‌های فروش — برای ساخت شاخص کیفیت لوکیشن (قیمت فروش هر متر مربع)"""
    df = pd.read_json(RAW_DIVAR)
    df = df[df.deal_type == "sale"].copy()
    for c in ["price_total", "area_sqm", "lat", "lng"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df.price_total.between(50, 5e7) & df.area_sqm.between(10, 2000)]
    df = df[df.lat.between(24, 40) & df.lng.between(43, 64)]
    df["type"] = [classify_type(t, cc) for t, cc in zip(df.title.fillna(""), df.category.fillna(""))]
    df = df[df.type.isin(["retail", "retail_mall", "commercial_other"])]
    df["psqm_sale"] = df.price_total * 1e6 / df.area_sqm
    # winsorise در سطح شهر
    q = df.groupby("city").psqm_sale.transform(
        lambda x: x.quantile(0.98) if len(x) >= 20 else np.nan)
    df = df[q.isna() | (df.psqm_sale <= q)]
    return df.reset_index(drop=True)
