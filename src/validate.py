"""اعتبارسنجی هولد-اوت + تنظیم پارامترهای مدل
هر آگهی هولد-اوت خودش را از مجموعه‌ی مشابه‌ها حذف می‌کند و مدل باید قیمت واقعی‌اش را بازتولید کند.
معیار: MdAPE (میانه خطای مطلق درصدی) + سهم پیش‌بینی‌های داخل ±۳۵٪
"""
from __future__ import annotations
import itertools, json, sys
from pathlib import Path
import numpy as np
import pandas as pd
from pipeline import clean_listings, clean_sales, load_stores, estimate_gamma, OUT
from valuation2 import Market

rng = np.random.default_rng(7)


def build_holdout(pool, stores, n_big=90, n_small=20):
    cities = set(stores.city)
    p = pool[pool.type.isin(["retail", "retail_mall"]) & pool.city.isin(cities)]
    p = p[p.area_sqm.between(15, 800)]
    parts = []
    for c, g in p.groupby("city"):
        k = n_big if len(g) > 150 else min(n_small, len(g))
        if k:
            parts.append(g.sample(min(k, len(g)), random_state=7))
    return pd.concat(parts)


def evaluate(mk: Market, ho, gamma):
    rows = []
    for _, r in ho.iterrows():
        e = mk.estimate(r.lat, r.lng, r.area_sqm, r.city, exclude_token=r.token)
        if not e.ok:
            rows.append((np.nan, r.eff_g, r.city, e.level)); continue
        rows.append((e.fair_cost, r.eff_g, r.city, e.level))
    d = pd.DataFrame(rows, columns=["pred", "actual", "city", "level"]).dropna()
    ape = (d.pred - d.actual).abs() / d.actual
    lerr = np.abs(np.log(d.pred / d.actual))
    return dict(mdape=float(ape.median()), mape=float(ape.mean()), mdlog=float(lerr.median()),
                within25=float((ape <= .25).mean()), within35=float((ape <= .35).mean()),
                cov=float(len(d) / len(ho)), n=len(d),
                by_city=d.assign(ape=ape).groupby("city").ape.median().round(3).to_dict())


def baseline(pool, ho):
    """مبنای مقایسه: میانه شهر × متراژ (بدون مدل مکانی)"""
    out = []
    for _, r in ho.iterrows():
        g = pool[(pool.city == r.city) & pool.type.isin(["retail", "retail_mall"]) & (pool.token != r.token)]
        if len(g) < 5:
            out.append((np.nan, r.eff_g)); continue
        k = np.median(g.eff_g.values / np.power(g.area_sqm.values, 0.9))
        out.append((k * r.area_sqm ** 0.9, r.eff_g))
    d = pd.DataFrame(out, columns=["pred", "actual"]).dropna()
    ape = (d.pred - d.actual).abs() / d.actual
    return dict(mdape=float(ape.median()), within35=float((ape <= .35).mean()), n=len(d))


def main():
    stores = load_stores()
    pool = clean_listings()
    sales = clean_sales()
    gamma, gstat = estimate_gamma(pool)
    ho = build_holdout(pool, stores)
    print(f"[i] holdout={len(ho)}  gamma={gamma:.3f}  sale_pool={len(sales)}")
    print("[i] baseline:", baseline(pool, ho))
    print("[i] v2 بدون LQ :", evaluate(Market(pool, sales, gamma, dict(use_lq=False)), ho, gamma))
    print("[i] v2 با LQ   :", evaluate(Market(pool, sales, gamma, dict(use_lq=True)), ho, gamma))

    grid = dict(band_km=[1.8, 2.6, 3.5], area_log_sd=[0.25, 0.35], shrink_k=[6.0], other_w=[0.3, 0.5, 0.7])
    best = None
    results = []
    for band, asd, sk, ow in itertools.product(grid["band_km"], grid["area_log_sd"], grid["shrink_k"], grid["other_w"]):
        cfg = dict(band_km=band, area_log_sd=asd, shrink_k=sk, other_w=ow)
        mk = Market(pool, sales, gamma, cfg)
        r = evaluate(mk, ho, gamma)
        results.append(dict(band_km=band, area_log_sd=asd, shrink_k=sk, other_w=ow, **r))
        if best is None or r["mdape"] < best[0]:
            best = (r["mdape"], cfg, r)
    res = pd.DataFrame(results).sort_values("mdape")
    print(res.head(10)[["band_km", "area_log_sd", "shrink_k", "other_w", "mdape", "mdlog", "within25", "within35", "cov"]].to_string(index=False))
    print("\n[✓] best:", best[1], {k: round(v, 3) if isinstance(v, float) else v for k, v in best[2].items() if k != "by_city"})
    (OUT / "validation.json").write_text(json.dumps(dict(
        holdout=int(len(ho)), gamma=gamma, gamma_stats=gstat,
        best=best[2], best_cfg=best[1],
        baseline=baseline(pool, ho),
        grid=res.to_dict(orient="records")), ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
