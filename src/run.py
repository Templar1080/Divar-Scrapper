"""اجرای نهایی پایپ‌لاین → out/app_data.json + out/store_scores.csv + out/methodology.md"""
from __future__ import annotations
import json, math
from pathlib import Path
import numpy as np
import pandas as pd
from pipeline import (CFG, OUT, load_stores, clean_listings, clean_sales, estimate_gamma,
                      store_economics, grade, wquantile)
from valuation2 import Market, TYPE_W, MALL_RE
import quality as Q
import trend as TREND
import units

TODAY = pd.Timestamp("2026-11-01")
BEST = dict(band_km=2.6, area_log_sd=0.35, shrink_k=6.0, other_w=0.50, use_lq=True)
W = dict(price=0.35, sales=0.25, deposit=0.15, contract=0.10, location=0.15)


# ---------------------------------------------------------------- امتیازها
def r0(v, nd=0):
    """گرد کردن ایمن: NaN → None"""
    if v is None: return None
    try:
        f = float(v)
    except Exception:
        return None
    if not math.isfinite(f): return None
    return round(f, nd) if nd else int(round(f))


def f_interp(x, xs, ys):
    return float(np.interp(x, xs, ys))


def fi(x, pts):
    xs, ys = zip(*pts)
    return float(np.interp(x, xs, ys))


def score_price(gap_med, gap_band, pct):
    if gap_med is None or not np.isfinite(gap_med):
        return 50.0
    a = fi(gap_med, [(-0.40, 100), (-0.20, 93), (-0.08, 84), (0, 76), (0.08, 70),
                      (0.18, 60), (0.30, 47), (0.50, 30), (0.80, 16), (1.50, 4)])
    b = 76.0
    if gap_band is not None and np.isfinite(gap_band):
        b = fi(gap_band, [(-0.50, 100), (-0.25, 92), (0, 78), (0.10, 66),
                           (0.25, 52), (0.45, 36), (0.75, 22), (1.20, 10)])
    return round(0.55 * a + 0.45 * b, 1)


def score_sales(rt, internal_med):
    """۶۰٪ معیار مطلق خرده‌فروشی (اجاره ÷ فروش)، ۴۰٪ نسبت به پرتفوی خودت"""
    if not rt or not np.isfinite(rt):
        return 50.0, None
    absolute = fi(rt, [(0.02, 100), (0.04, 92), (0.06, 80), (0.08, 66),
                           (0.10, 52), (0.14, 32), (0.20, 14), (0.35, 3)])
    rel = rt / internal_med if internal_med else 1.0
    relative = fi(rel, [(0.5, 100), (0.8, 88), (1.0, 74), (1.25, 58), (1.6, 38), (2.2, 18)])
    return round(0.6 * absolute + 0.4 * relative, 1), round(rt, 4)


def score_deposit(dep, eff, market_dep_months):
    if not dep or not eff:
        return 72.0, None
    months = dep / max(eff, 1)
    ref = market_dep_months if (market_dep_months and np.isfinite(market_dep_months)) else 6.0
    rel = months / max(ref, 0.5)
    return round(fi(rel, [(0, 100), (0.5, 94), (0.9, 82), (1.3, 68), (2, 48), (3, 26), (5, 10)]), 1), round(months, 2)


def score_contract(renewal, inc, today=TODAY):
    try:
        ren = pd.Timestamp(renewal)
        months = (ren - today).days / 30.44
    except Exception:
        months = None
    s = 62.0
    if months is not None:
        s = fi(months, [(-3, 20), (0, 34), (6, 48), (12, 60), (24, 70), (36, 78), (60, 86)])
    if inc is None:
        s -= 4
    elif inc <= 15: s += 10
    elif inc <= 25: s += 4
    elif inc <= 35: s -= 5
    else: s -= 13
    return round(float(np.clip(s, 0, 100)), 1), (None if months is None else round(months, 1))


def score_location(est, comp_score, depth_score, lq_pct):
    return round(float(np.clip(0.40 * comp_score + 0.30 * depth_score + 0.30 * lq_pct, 0, 100)), 1)


def conf_of(est, n_hold):
    c_n = f_interp(est.effN, [0, 3, 6, 10, 18, 30, 60], [15, 38, 55, 68, 80, 90, 97])
    c_s = f_interp(est.sigma_log if np.isfinite(est.sigma_log) else 0.6, [0.15, 0.3, 0.45, 0.6, 0.85], [100, 88, 72, 55, 35])
    c_e = 55 + 45 * (est.exact_share or 0)
    c_l = {"local": 100, "hybrid": 82, "city": 52}.get(est.level, 50)
    return round(float(np.clip(0.28 * c_n + 0.24 * c_s + 0.16 * c_e + 0.32 * c_l, 0, 100)), 1)


def verdict(pct, gap_band):
    if pct is None or not np.isfinite(pct):
        return ("نامشخص", "داده کافی برای قضاوت نیست")
    if pct <= 0.15: return ("خیلی زیر بازار", "اجاره‌ی شما در پایین‌ترین ۱۵٪ بازار محلی است — فرصت خوب؛ ریسک افزایش شدید در تمدید")
    if pct <= 0.35: return ("زیر بازار", "اجاره‌ی شما پایین‌تر از میانه‌ی بازار محلی است")
    if pct <= 0.70: return ("هم‌تراز بازار", "اجاره در بازه‌ی طبیعی بازار محلی است")
    if pct <= 0.85: return ("بالای بازار", "کمی بالاتر از میانه‌ی بازار؛ در مذاکره قابل کاهش است")
    if pct <= 0.95: return ("گران", "اجاره بالاتر از ۸۵٪ مشابه‌های محلی است — اولویت مذاکره")
    return ("بسیار گران", "اجاره در بالاترین ۵٪ بازار محلی است — بازبینی جدی قرارداد")


def sensitivity(stores, gamma, rates=(0.02, 0.04)):
    """اثر تغییر نرخ تبدیل ودیعه به اجاره روی «اختلاف با بازار» هر فروشگاه"""
    out = {}
    for rate in rates:
        CFG["rahn_rate"] = rate
        pool_r = clean_listings()
        sale_r = clean_sales()
        mk_r = Market(pool_r, sale_r, gamma, BEST)
        for i, st in stores.iterrows():
            e = store_economics(st)
            is_mall = bool(MALL_RE.search(f"{st.store_name} {st.neighborhood_or_street}"))
            est = mk_r.estimate(st.lat, st.lng, st.area_sqm, st.city,
                                actual_cost_ground=e["effective"], mall=is_mall)
            out.setdefault(st.store_id, {})[rate] = dict(
                gap=r0(est.gap_vs_med, 3), pct=r0(est.percentile, 3), fair=r0(est.fair_cost),
                eff=r0(e["effective"]))
    CFG["rahn_rate"] = 0.03
    return out


def recommend(st_score, pct, gap, rts, internal_med, months_left, conf):
    """اقدام پیشنهادی عملی برای هر فروشگاه"""
    acts = []
    if conf < 65:
        acts.append("اول داده‌ی محلی را کامل کن (اسکرپ آگهی بیشتر در همین محله) تا قضاوت مطمئن شود")
    if pct is not None and pct > 0.9:
        acts.append("مذاکره‌ی جدی برای کاهش اجاره یا دریافت امتیاز (خدمات/تریج/تجهیزات) — اجاره بالای ۹۰٪ بازار محلی است")
    elif pct is not None and pct > 0.75:
        acts.append("در تمدید بعدی روی کاهش ۱۰–۲۰٪ چانه بزن؛ مستندات همین تحلیل را ضمیمه کن")
    if rts is not None and internal_med and rts > 1.35 * internal_med:
        acts.append("نسبت اجاره به فروش بالاتر از پرتفوی است: بازبینی لوکیشن یا افزایش بهره‌وری فروش")
    if months_left is not None and months_left < 12 and pct is not None and pct < 0.4:
        acts.append("اجاره زیر بازار است و تمدید نزدیک — پیشنهاد تمدید بلندمدت با سقف افزایش مشخص قبل از جهش")
    if pct is not None and pct < 0.25:
        acts.append("ارزش خوب: در تمدید، حفظ قرارداد اولویت دارد")
    if not acts:
        acts.append("وضعیت متعادل — پایش سالانه کافی است")
    return acts


def main():
    stores = load_stores()
    pool = clean_listings()
    sales = clean_sales()
    gamma, gstat = estimate_gamma(pool)
    mk = Market(pool, sales, gamma, BEST)
    print(f"[i] gamma={gamma:.3f}  beta_LQ={mk.beta:.3f}  rent={len(pool)} sale={len(sales)}")

    # ---------- آمار شهرها برای ملاحظه‌ی توسعه
    retail = pool[pool.type.isin(["retail", "retail_mall"])].copy()
    cities = {}
    for c, g in retail.groupby("city"):
        if len(g) < 8: continue
        lq_pct = None
        cost150 = g.k_raw.values * 150 ** (gamma - 1)
        cities[c] = dict(
            n=int(len(g)), n_all=int((pool.city == c).sum()),
            psqm=[round(float(g.k_raw.quantile(q))) for q in (0.25, 0.5, 0.75, 0.9)],
            cost150=[round(float(np.quantile(cost150, q))) for q in (0.25, 0.5, 0.75)],
            area_med=round(float(g.area_sqm.median()), 1),
            dep_months=round(float((g.dep / g.eff.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna().median()), 1),
            lat=round(float(g.lat.median()), 4), lng=round(float(g.lng.median()), 4),
            nbh={nb: dict(n=int(len(x)),
                          psqm=round(float(x.k_raw.median())),
                          cost150=round(float(np.median(x.k_raw.values * 150 ** (gamma - 1)))),
                          lat=round(float(x.lat.median()), 4), lng=round(float(x.lng.median()), 4))
                 for nb, x in g.groupby("neighborhood") if len(x) >= 5},
        )
    all_dep_months = float(np.median([c["dep_months"] for c in cities.values() if np.isfinite(c["dep_months"])]))
    # درصد جایگاه LQ در شهر (کیفیت لوکیشن)
    lq_by_city = {}
    for c, g in pool.groupby("city"):
        lqs = g.lq.dropna().values if "lq" in g else np.array([])

    # ---------- حساسیت به نرخ رهن
    sens = sensitivity(stores, gamma)

    # ---------- اقتصاد و امتیاز فروشگاه‌ها
    econ = pd.DataFrame([store_economics(s) for s in stores.itertuples()])
    internal_med = float(np.nanmedian(econ.rent_to_sales.values))

    # ---------- مدل کیفیت بنگاه (طبقه/موقعیت/نبش/ویترین)
    QMODEL = Q.build_model()
    QF = QMODEL["factors"]

    ATTR_FILE = OUT / "ویژگی_فروشگاه‌ها.csv"
    attr_over = {}
    if ATTR_FILE.exists():
        try:
            _a = pd.read_csv(ATTR_FILE)
            for _, r in _a.iterrows():
                attr_over[str(r["store_id"])] = dict(
                    floor=str(r.get("floor") or "ground").strip() or "ground",
                    position=str(r.get("position") or "street").strip() or "street",
                    corner=str(r.get("corner", "")).strip().lower() in ("1", "true", "بله", "yes", "y"),
                    frontage=str(r.get("frontage") or "normal").strip() or "normal",
                    visibility=str(r.get("visibility") or "normal").strip() or "normal")
        except Exception as _e:
            print("[!] ویژگی_فروشگاه‌ها.csv خوانده نشد:", _e)

    BRAND_MALL = ("مایلی", "هایپر", "هایپرمارکت", "مال ", "مگامال", "سنتر")

    def store_attrs(s):
        """ویژگی‌های پیش‌فرض هر فروشگاه؛ در داشبورد و در CSV قابل ویرایش است."""
        nm = f"{s.store_name} {s.neighborhood_or_street}"
        is_mall = bool(MALL_RE.search(nm)) or any(b in nm for b in BRAND_MALL)
        base = dict(floor="ground", position=("mall" if is_mall else "street"),
                    corner=False, frontage="normal", visibility="normal",
                    src="default", mall=bool(is_mall))
        ov = attr_over.get(str(s.store_id))
        if ov:
            base.update(ov); base["src"] = "csv"
        return base

    out_stores = []
    for i, s in stores.iterrows():
        e = econ.iloc[i]
        eff_psqm = e.effective / s.area_sqm
        is_mall = bool(MALL_RE.search(f"{s.store_name} {s.neighborhood_or_street}"))
        attrs = store_attrs(s)
        qf = Q.qual(attrs, QF)          # ضریب کیفیت (۱ = همکفِ بر خیابان)
        est_own = mk.estimate(s.lat, s.lng, s.area_sqm, s.city, exclude_token=None,
                              actual_cost_ground=e.effective, mall=is_mall)
        # تخمینِ تعدیل‌شده: هزینه‌ی واقعی به «معادل همکفِ بر خیابان» تبدیل می‌شود
        est = mk.estimate(s.lat, s.lng, s.area_sqm, s.city, exclude_token=None,
                          actual_cost_ground=(e.effective / qf if qf > 0 else e.effective),
                          mall=is_mall)
        cs = cities.get(s.city, {})
        # مشابه‌ها
        comps = mk.comparables(s.lat, s.lng, s.area_sqm, s.city, radius_km=5.0).head(400)
        comp_list = []
        for _, r in comps.head(16).iterrows():
            comp_list.append(dict(
                t=str(r.title)[:80], a=round(float(r.area_sqm), 1), rt=round(float(r.rent)),
                dp=round(float(r.dep)), ef=round(float(r.eff)), d=round(float(r.dist), 2),
                fl=str(r.floor) if pd.notna(r.floor) else "", ty=r.type,
                p=round(float(r.k_raw)), u=str(r.url)))
        scatter = [[round(float(r.area_sqm), 1), round(float(r.k_raw)), round(float(r.dist), 2)]
                   for _, r in comps.sample(min(220, len(comps)), random_state=7).iterrows()] if len(comps) else []
        # امتیازها
        sp = score_price(est.gap_vs_med, est.gap_vs_band, est.percentile)
        ss, rts = score_sales(e.rent_to_sales, internal_med)
        sd, dep_months = score_deposit(e.deposit, e.effective, cs.get("dep_months") or all_dep_months)
        sc_, months_left = score_contract(e["renewal"], e["inc"])
        comp_n = est.competitors
        comp_score = f_interp(comp_n, [0, 2, 5, 10, 20, 40], [58, 76, 80, 70, 55, 40])
        depth_score = f_interp(min(est.n_local, 200), [0, 10, 30, 80, 150], [40, 60, 76, 88, 94])
        lq_pct = 50.0
        if np.isfinite(est.lq) and s.city in lq_by_city:
            pass
        lq_score = 50.0
        if np.isfinite(est.lq):
            # کیفیت لوکیشن: درصد جایگاه نسبت به شهر (از توزیع آگهی‌های فروش)
            lq_ref = mk.city_lq.get(s.city)
            if lq_ref and np.isfinite(lq_ref):
                lq_score = f_interp((est.lq - lq_ref) / 0.35, [-1.5, -0.5, 0, 0.5, 1.5], [30, 45, 62, 78, 92])
        sl = score_location(est, comp_score, depth_score, lq_score)
        for name, v in (("sp", sp), ("ss", ss), ("sd", sd), ("sc", sc_), ("sl", sl)):
            pass
        sp = sp if np.isfinite(sp) else 50.0
        ss = ss if np.isfinite(ss) else 50.0
        sd = sd if np.isfinite(sd) else 72.0
        sc_ = sc_ if np.isfinite(sc_) else 55.0
        sl = sl if np.isfinite(sl) else 55.0
        # وزن‌های پویا: در قرارداد درصدی از فروش، معیار اصلی «نسبت اجاره به فروش» است
        if e["structure"] == "fixed":
            wts = dict(price=0.35, sales=0.22, dep=0.15, contract=0.13, location=0.15)
        else:
            wts = dict(price=0.22, sales=0.36, dep=0.14, contract=0.13, location=0.15)
        total = (wts["price"] * sp + wts["sales"] * ss + wts["dep"] * sd
                 + wts["contract"] * sc_ + wts["location"] * sl)
        conf = conf_of(est, None)
        unc = round(3 + 13 * (1 - conf / 100), 1)
        v_label, v_text = verdict(est.percentile, est.gap_vs_band)

        flags = list(e["flags"])
        if months_left is not None and months_left < 8:
            flags.append("تمدید قرارداد نزدیک است")
        if e["structure"] == "hybrid" and not e["min_rent"]:
            flags.append("حداقل اجاره ثبت نشده")
        if est.level == "city":
            flags.append("آگهی مغازه‌ی کافی در محدوده نیست — تخمین کم‌دقت‌تر")
        if is_mall:
            flags.append("واحد داخل مجتمع/مال — مقایسه با مغازه‌های خیابانی فقط تقریبی است")
        if e["structure"] != "fixed":
            flags.append("قرارداد درصدی/ترکیبی: مقایسه با اجاره‌ی ثابت بازار باید با احتیاط خوانده شود")

        out_stores.append(dict(
            id=s.store_id, name=str(s.store_name), city=str(s.city), nb=str(s.neighborhood_or_street),
            lat=round(float(s.lat), 6), lng=round(float(s.lng), 6), area=round(float(s.area_sqm), 1),
            deal=dict(structure=e["structure"], cash=round(e["cash_rent"]), dep=round(e["deposit"]),
                      dep_cost=round(e["deposit_cost"]), eff=round(e["effective"]),
                      min_rent=round(e["min_rent"]), pct=e["pct"],
                      sales=None if e["sales"] is None else round(e["sales"])),
            psqm=round(float(eff_psqm)), rts=rts,
            rts_fair=(round(est.fair_cost / e["sales"], 4) if (e["sales"] and est.ok) else None),
            rts_max=(round(0.06 * e["sales"]) if e["sales"] else None),
            contract=dict(start=e["lease_start"], renewal=e["renewal"], inc=r0(e["inc"], 1),
                          months_left=r0(months_left, 1), dep_months=r0(dep_months, 2)),
            market=dict(ok=bool(est.ok), level=est.level, n=est.n_local, effN=round(est.effN, 1),
                        fair=r0(est.fair_cost), p25=r0(est.p25), p75=r0(est.p75), p90=r0(est.p90),
                        ci_lo=r0(est.ci_lo), ci_hi=r0(est.ci_hi),
                        gap_med=None if not np.isfinite(est.gap_vs_med) else round(est.gap_vs_med, 4),
                        gap_band=None if not np.isfinite(est.gap_vs_band) else round(est.gap_vs_band, 4),
                        pct=None if not np.isfinite(est.percentile) else round(est.percentile, 3),
                        sigma=None if not np.isfinite(est.sigma_log) else round(est.sigma_log, 3),
                        competitors=est.competitors, lq=None if not np.isfinite(est.lq) else round(est.lq, 3),
                        lq_used=est.lq_used, city_psqm=(cs.get("psqm") or [None,None])[1],
                        city_dep_months=cs.get("dep_months"), median_dist=None if not np.isfinite(est.median_dist_km) else round(est.median_dist_km, 2)),
            scores=dict(total=round(total, 1), unc=unc, conf=conf, grade=grade(total),
                        price=round(sp, 1), sales=round(ss, 1), dep=round(sd, 1),
                        contract=round(sc_, 1), location=round(sl, 1)),
            weights=wts, mall=is_mall,
            verdict=dict(label=v_label, text=v_text),
            sens=sens.get(s.store_id),
            actions=recommend(total, est.percentile, est.gap_vs_med, e["rent_to_sales"],
                              internal_med, months_left, conf),
            flags=flags, comps=comp_list, scatter=scatter,
            attrs=attrs, qual=round(qf, 3),
            fair_ref=r0(est.fair_cost), fair_own=r0(est.fair_cost * qf),
            qk=[round(float(np.quantile(comps.k_raw.values / comps.area_sqm.values ** (gamma - 1), p)), 3)
                for p in np.linspace(.02, .98, 25)] if len(comps) else [],
        ))

    tot = pd.Series([r["scores"]["total"] for r in out_stores])
    val = json.loads((OUT / "validation.json").read_text()) if (OUT / "validation.json").exists() else {}
    meta = dict(
        generated="2026-11-01", n_sources=2,
        n_listings=int(len(pool) + len(sales)), n_rent=int(len(pool)), n_sale=int(len(sales)),
        n_retail=int(len(retail)), n_comps_total=int(len(pool[pool.type.isin(["retail", "retail_mall"])])),
        gamma=round(gamma, 3), beta_lq=round(mk.beta, 3), rahn_rate=CFG["rahn_rate"],
        internal_median_rts=round(internal_med, 4), market_dep_months=round(all_dep_months, 2),
        weights=W, n_stores=len(out_stores),
        score_med=round(float(tot.median()), 1), score_mean=round(float(tot.mean()), 1),
        val=dict(mdape=val.get("best", {}).get("mdape"), baseline=val.get("baseline", {}).get("mdape"),
                 within35=val.get("best", {}).get("within35"), n=val.get("holdout")),
        cities_n=len(cities),
    )

    # ---------- لیستینگ‌ها برای نقشه/جست‌وجو (فقط مغازه‌ها)
    lis = retail[["token", "title", "city", "neighborhood", "lat", "lng", "area_sqm",
                  "eff", "dep", "rent", "k_raw", "type", "url", "floor"]].copy()
    lis["t"] = lis.title.fillna("").str.slice(0, 70)
    payload_list = [[r.token, r.city, r.neighborhood, round(r.lat, 5), round(r.lng, 5),
                     round(r.area_sqm, 1), round(r.eff), round(r.dep), round(r.rent),
                     round(r.k_raw), r.type, r.url, r.t, str(r.floor) if pd.notna(r.floor) else ""]
                    for r in lis.itertuples()]

    # ---------- ممیزی واحد + مدل کیفیت + روند
    units_audit = dict(
        verdict="تومان",
        checks=[
            dict(k="ودیعه/اجاره‌ی خوانده‌شده از صفحه‌ی آگهی", v="تومان"),
            dict(k="قیمت کل فروش (price_total)", v="میلیون تومان"),
            dict(k="k_raw / psqm", v="تومان بر متر مربع در ماه"),
            dict(k="ستون‌های فایل فروشگاه‌ها", v="تومان"),
            dict(k="نرخ تبدیل ودیعه→اجاره", v=f"{CFG['rahn_rate']} ماهانه"),
        ],
        evidence=[
            dict(k="مغازه‌ی ۱۰۰ متری تهران — میانه‌ی ودیعه", v="۱٫۵ میلیارد تومان"),
            dict(k="مغازه‌ی ۱۰۰ متری تهران — میانه‌ی اجاره", v="۳۳ میلیون تومان/ماه"),
            dict(k="اگر ارقام ریال بود، همان مغازه", v="۱۵۰ میلیون ودیعه و ۳٫۳ میلیون اجاره → غیرواقعی"),
            dict(k="نسبت اجاره‌ی فروشگاه شما به میانه‌ی محله", v="۹۴٪ (یعنی هم‌مقیاس، نه ۱۰ برابر)"),
        ],
        rial_quoted_share="۰٫۲۳٪ آگهی‌ها عدد را ریال نوشته‌اند و در پاک‌سازی ÷۱۰ می‌شوند",
        note=units.NOTE,
    )
    trend_payload = TREND.build() if (Path(__file__).parent / "trend.py").exists() else None
    payload = dict(meta=meta, cities=cities, stores=out_stores, listings=payload_list,
                   quality=QMODEL, units=units_audit, trend=trend_payload,
                   cols=["token", "city", "nb", "lat", "lng", "area", "eff", "dep", "rent", "psqm", "type", "url", "title", "floor"])
    txt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    (OUT / "app_data.json").write_text(txt, encoding="utf-8")
    print(f"[✓] app_data.json  {(OUT/'app_data.json').stat().st_size/1e6:.2f} MB  listings={len(payload_list)}")

    # ---------- قالب ویژگی فروشگاه‌ها (برای پر کردن دستی → دقت بیشتر)
    tmpl = pd.DataFrame([dict(
        store_id=r["id"], store=r["name"], city=r["city"],
        floor=r["attrs"]["floor"], position=r["attrs"]["position"],
        corner=int(bool(r["attrs"]["corner"])), frontage=r["attrs"]["frontage"],
        visibility=r["attrs"]["visibility"],
        راهنما_floor="ground|f1|f2|f3|f4p|basement",
        راهنما_position="street|passage|mall|alley",
        راهنما_corner="0|1 (نبش یا دوبر)",
        راهنما_frontage="narrow|normal|wide|xwide",
        راهنما_visibility="hidden|normal|prime",
    ) for r in out_stores])
    tmpl_path = OUT / "قالب_ویژگی_فروشگاه‌ها.csv"
    tmpl.to_csv(tmpl_path, index=False, encoding="utf-8-sig")

    # ---------- CSV
    rows = []
    for r in out_stores:
        m, sc = r["market"], r["scores"]
        rows.append(dict(store=r["name"], city=r["city"], nb=r["nb"], area=r["area"],
                         structure=r["deal"]["structure"], cash_rent=r["deal"]["cash"],
                         deposit=r["deal"]["dep"], deposit_monthly=r["deal"]["dep_cost"],
                         effective_monthly=r["deal"]["eff"], psqm=r["psqm"],
                         monthly_sales=r["deal"]["sales"], rent_to_sales=r["rts"],
                         fair_monthly=m["fair"], fair_low=m["p25"], fair_high=m["p75"],
                         actual_vs_median_pct=None if m["gap_med"] is None else round(m["gap_med"] * 100, 1),
                         over_band_pct=None if m["gap_band"] is None else round(m["gap_band"] * 100, 1),
                         market_percentile=None if m["pct"] is None else round(m["pct"] * 100),
                         comps=m["n"], competitors_2km=m["competitors"],
                         verdict=r["verdict"]["label"], score=r["scores"]["total"], grade=sc["grade"],
                         s_price=sc["price"], s_sales=sc["sales"], s_deposit=sc["dep"],
                         s_contract=sc["contract"], s_location=sc["location"],
                         confidence=sc["conf"], renewal=r["contract"]["renewal"],
                         annual_increase=r["contract"]["inc"], flags=" | ".join(r["flags"])))
    df = pd.DataFrame(rows).sort_values("score", ascending=False)
    df.to_csv(OUT / "store_scores.csv", index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(OUT / "نتیجه_تحلیل_فروشگاه‌ها.xlsx", engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="امتیازها", index=False)
    print(df[["store", "city", "verdict", "actual_vs_median_pct", "market_percentile", "score", "grade", "confidence"]].to_string(index=False))
    print("\nmeta:", {k: v for k, v in meta.items() if k not in ("weights",)})


if __name__ == "__main__":
    main()
