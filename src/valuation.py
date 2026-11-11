"""
موتور ارزش‌گذاری — تخمین هزینه ماهانه منصفانه برای یک نقطه/متراژ مشخص
=======================================================================
معماری چهارسطحی (چون داده در همه شهره به یک اندازه انبوه نیست):

  سطح ۱ (local) : مشابه‌های همسایه با کرنل مکانی/متراژی + میانه وزنی (robust)
  سطح ۲ (hybrid): ترکیب سطح ۱ و سطح ۳ با وزن = f(effective N)
  سطح ۳ (hood)  : میانه محله با انقباض تجربی-بیزی به سمت شهر
  سطح ۴ (city)  : میانه شهر (پیشین نهایی)

قرارداد ریاضی:  k = هزینه‌ی ماهانه معادل‌همکف ÷ متراژ^γ
  γ از رگرسیون هابر روی خود بازار برآورد می‌شود (نه دستی)
  تخمین = exp(میانه‌ی وزنی log k) × متراژ^γ
"""
from __future__ import annotations
import math
from dataclasses import dataclass
import numpy as np
import pandas as pd
from pipeline import haversine, wquantile, wmedian_mad, CFG

TYPE_W = {"retail": 1.0, "retail_mall": 0.88, "commercial_other": 0.45,
          "food_service": 0.5, "service": 0.5, "office": 0.0, "industrial": 0.0,
          "residential": 0.0}

DEFAULT = dict(
    band_km=1.6,
    band_km_max=6.0,
    area_log_sd=0.34,
    gps_sigma_km=0.45,
    shrink_k=5.0,
    gamma=0.9,
    other_w=0.45,        # وزن آگهی‌های «تجاری نامشخص»
)

COMP_RE = "سوپرمارکت|سوپر مارکت|هایپر|میوه|مواد غذایی|بقالی"


@dataclass
class Est:
    ok: bool
    fair_cost: float = float("nan")
    fair_psqm: float = float("nan")
    effN: float = 0.0
    sigma_log: float = float("nan")
    level: str = ""
    n_local: int = 0
    p25: float = float("nan")
    p75: float = float("nan")
    ci_lo: float = float("nan")
    ci_hi: float = float("nan")
    exact_share: float = 0.0
    median_dist_km: float = float("nan")
    competitors: int = 0
    city_psqm: float = float("nan")
    reason: str = ""
    comps: object = None
    p90: float = float("nan")
    percentile: float = float("nan")   # جایگاه اجاره‌ی فعلی در توزیع مشابه‌های محلی
    gap_vs_med: float = float("nan")
    gap_vs_band: float = float("nan")  # نسبت به سقف باند (p75) — برای لوکیشن‌های پرایم


class Market:
    """نمونه‌ی بازار آماده‌ی پرس‌وجو (پیش‌محاسبه‌شده برای سرعت)"""

    def __init__(self, pool: pd.DataFrame, gamma: float, cfg: dict | None = None):
        self.cfg = {**DEFAULT, **(cfg or {})}
        self.cfg["gamma"] = gamma
        g = self.cfg["gamma"]
        p = pool[pool.type.map(TYPE_W).fillna(0) > 0].copy()
        p["k"] = p.eff_g / np.power(p.area_sqm.values, g)
        p = p[np.isfinite(p.k) & (p.k > 0)]
        self.df = p
        self.other_w = self.cfg["other_w"]
        self._cities = {}
        for c, gg in p.groupby("city"):
            tw = gg.type.map(TYPE_W).values.astype(float)
            tw = np.where(gg.type.values == "commercial_other", self.other_w, tw)
            self._cities[c] = dict(
                lat=gg.lat.values, lng=gg.lng.values, area=gg.area_sqm.values,
                logk=np.log(gg.k.values), exact=gg.exact.values.astype(float),
                gk=gg.ground_known.values.astype(float), tw=tw,
                token=gg.token.values, idx=np.arange(len(gg)),
                is_comp=gg.title.fillna("").str.contains(COMP_RE).values)
        # پیشین سطح شهر
        self.city_prior = {}
        for c, gg in p.groupby("city"):
            if len(gg) < 5: continue
            lk = np.log(gg.k.values)
            med = float(np.median(lk))
            self.city_prior[c] = dict(logk=med, n=int(len(gg)),
                                      sigma=float(1.4826 * np.median(np.abs(lk - med))),
                                      psqm=float(np.exp(med) * gg.area_sqm.median() ** (g - 1)),
                                      nbh={})
        # پیشین سطح محله (>=3 آگهی)
        for (c, nb), gg in p.groupby(["city", "neighborhood"]):
            cp = self.city_prior.get(c)
            if cp is None or len(gg) < 3: continue
            lk = np.log(gg.k.values)
            med = float(np.median(lk))
            cp["nbh"][str(nb)] = dict(n=int(len(gg)), logk=med,
                                      sigma=float(1.4826 * np.median(np.abs(lk - med))))
        if self.city_prior:
            allk = np.log(p.k.values)
            self.glob = dict(logk=float(np.median(allk)),
                             sigma=float(1.4826 * np.median(np.abs(allk - np.median(allk)))),
                             n=int(len(p)))
        else:
            self.glob = dict(logk=0.0, sigma=0.5, n=0)

    # ------------------------------------------------------------------ سطح محلی
    def _local(self, city, lat, lng, area, band, exclude_token=None):
        c = self._cities.get(city)
        if c is None: return None
        d = haversine(lat, lng, c["lat"], c["lng"])
        rmax = max(band * 2.3, 1.0)
        m = d <= rmax
        if exclude_token is not None:
            m &= (c["token"] != exclude_token)
        if m.sum() == 0: return None
        dd = d[m]
        sig = np.where(c["exact"][m] >= 1, 0.0, self.cfg["gps_sigma_km"])
        d_eff = np.sqrt(dd ** 2 + sig ** 2)
        w = np.exp(-0.5 * (d_eff / band) ** 2)
        lr = np.log(c["area"][m] / area)
        w *= np.exp(-0.5 * (lr / self.cfg["area_log_sd"]) ** 2)
        w *= np.where(c["gk"][m] > 0, 1.0, 0.85)
        w *= c["tw"][m]
        if w.sum() <= 0: return None
        lk = c["logk"][m]
        med, mad = wmedian_mad(lk, w)
        effn = float(w.sum() ** 2 / (w ** 2).sum())
        kk = np.exp(lk)
        return dict(logk=med, effn=effn, sigma=mad if np.isfinite(mad) else self.glob["sigma"],
                    p25=wquantile(kk, w, 0.25), p75=wquantile(kk, w, 0.75),
                    p90=wquantile(kk, w, 0.90),
                    exact=float(c["exact"][m].mean()), mdist=float(np.median(dd)),
                    n=int(m.sum()),
                    competitors=int(np.sum((dd <= 2.0) & c["is_comp"][m])),
                    mask=m, dist=dd, w=w, lk=lk,
                    wsum=float(w.sum()))

    # ------------------------------------------------------------------ تخمین
    def estimate(self, lat, lng, area, city, nb="", exclude_token=None, cfg=None,
                 actual_cost_ground: float | None = None) -> Est:
        if cfg: self.cfg.update(cfg)
        cfg_ = self.cfg
        g = cfg_["gamma"]
        if not (area and area > 0):
            return Est(ok=False, reason="متراژ نامعتبر")
        cp = self.city_prior.get(city)
        if cp is None:
            return Est(ok=False, reason="این شهر در دیتاست نیست")
        # باند تطبیقی
        loc = None
        band_used = cfg_["band_km"]
        for band in (cfg_["band_km"], cfg_["band_km"] * 2, cfg_["band_km_max"]):
            loc = self._local(city, lat, lng, area, band, exclude_token)
            band_used = band
            if loc and loc["effn"] >= 8: break
        # سطح محله با انقباض
        nbp = cp["nbh"].get(str(nb))
        if nbp:
            a = nbp["n"] / (nbp["n"] + 4.0)
            logk_hood = a * nbp["logk"] + (1 - a) * cp["logk"]
        else:
            logk_hood = cp["logk"]

        if loc:
            wl = loc["effn"] / (loc["effn"] + cfg_["shrink_k"])
            logk = wl * loc["logk"] + (1 - wl) * logk_hood
            sigma = math.sqrt(wl ** 2 * loc["sigma"] ** 2 / max(loc["effn"], 1)
                              + (1 - wl) ** 2 * cp["sigma"] ** 2)
            se = max(0.05, 1.2533 * max(loc["sigma"], cp["sigma"]) *
                     math.sqrt(1.0 / max(loc["effn"], 1) + 1.0 / max(cp["n"], 1)))
            level = "local" if wl >= 0.6 else "hybrid"
        else:
            logk, sigma = logk_hood, cp["sigma"]
            se = max(0.12, max(cp["sigma"], 0.25) * math.sqrt(1 / 15 + 1 / max(cp["n"], 1)))
            level = "city"
        fair = math.exp(logk) * area ** g
        p90 = loc["p90"] * area ** g if loc and "p90" in loc else fair
        pct = float("nan")
        gap_med = float("nan")
        gap_band = float("nan")
        if actual_cost_ground is not None and actual_cost_ground > 0:
            k_act = math.log(actual_cost_ground / area ** g)
            gap_med = actual_cost_ground / fair - 1.0
            if loc:
                lk_, w_ = loc["lk"], loc["w"]
                pct = float(w_[lk_ <= k_act].sum() / max(loc["wsum"], 1e-9))
                p75_cost = loc["p75"] * area ** g
                gap_band = actual_cost_ground / p75_cost - 1.0 if p75_cost > 0 else float("nan")
        return Est(ok=True, fair_cost=fair, fair_psqm=fair / area, effN=(loc["effn"] if loc else 0.0),
                   p90=p90, percentile=pct, gap_vs_med=gap_med, gap_vs_band=gap_band,
                   sigma_log=float(sigma), level=level, n_local=(loc["n"] if loc else 0),
                   p25=(loc["p25"] * area ** g if loc else fair),
                   p75=(loc["p75"] * area ** g if loc else fair),
                   ci_lo=fair * math.exp(-1.96 * se), ci_hi=fair * math.exp(1.96 * se),
                   exact_share=(loc["exact"] if loc else 0.0),
                   median_dist_km=(loc["mdist"] if loc else float("nan")),
                   competitors=(loc["competitors"] if loc else 0),
                   city_psqm=cp["psqm"], comps=loc)

    # ------------------------------------------------------------------ مشابه‌ها برای نمایش
    def comparables(self, lat, lng, area, city, k=12, radius_km=5.0):
        c = self._cities.get(city)
        if c is None: return pd.DataFrame()
        d = haversine(lat, lng, c["lat"], c["lng"])
        m = d <= radius_km
        if m.sum() == 0: return pd.DataFrame()
        sub = self.df[self.df.city == city].iloc[np.where(m)[0]]
        sub = sub.assign(dist=d[m], wrank=np.exp(-0.5 * (d[m] / 2.0) ** 2)
                         * np.exp(-0.5 * (np.log(sub.area_sqm.values / area) / self.cfg["area_log_sd"]) ** 2))
        return sub.sort_values("wrank", ascending=False)
