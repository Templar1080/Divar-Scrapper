"""
موتور ارزش‌گذاری نسخه ۲ — هدانیک با «شاخص کیفیت لوکیشن» (LQ)
=============================================================
مشکل نسخه ۱: در محله‌هایی که آگهی اجاره‌ی مغازه کم است، تخمین به میانه‌ی شهر برمی‌گشت
            و تفاوت محله‌ها را از دست می‌داد.

راه‌حل نسخه ۲: از ۸۰ هزار آگهی *فروش* هم استفاده می‌کنیم. قیمت فروش هر متر مربع،
            کیفیت لوکیشن را می‌سنجد (همبستگی log–log ≈ ۰.۶۵ در سطح سلول ۱ کیلومتری).

مدل:
    log k = α(city) + β·ΔLQ(point) + w·r_local + ε
    که  k = هزینه‌ماهانه‌ی معادل‌همکف ÷ متراژ^γ
          ΔLQ = انحراف شاخص کیفیت لوکیشن نقطه از میانگین شهر
          r_local = میانه‌ی وزنی پسماند آگهی‌های اجاره‌ی اطراف (با انقباض به صفر)
          w = effN/(effN+k0)  → با داده‌ی کم، وزن حدس شهری غالب می‌شود (نه فروپاشی)
"""
from __future__ import annotations
import math, re
from dataclasses import dataclass
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from pipeline import haversine, wquantile, wmedian_mad

TYPE_W = {"retail": 1.0, "retail_mall": 0.88, "commercial_other": 0.45,
          "food_service": 0.5, "service": 0.5, "office": 0.0, "industrial": 0.0,
          "residential": 0.0}
COMP_RE = "سوپرمارکت|سوپر مارکت|هایپر|میوه|مواد غذایی|بقالی"
MALL_RE = re.compile(r"(?<!ش)مال(?![ی\u200c])|پاساژ|مجتمع تجاری|سنتر|سیتی|بازار|طبقه|فودکورت", re.I)

DEFAULT = dict(
    band_km=1.5,          # کرنل آگهی‌های اجاره
    band_max_km=6.0,
    lq_band_km=1.2,       # کرنل شاخص کیفیت لوکیشن
    lq_max_km=6.0,
    area_log_sd=0.35,
    gps_sigma_km=0.45,
    shrink_k=6.0,
    other_w=0.30,
    gamma=0.9,
    use_lq=True,
)


@dataclass
class Est:
    ok: bool
    fair_cost: float = float("nan")
    fair_psqm: float = float("nan")
    effN: float = 0.0
    level: str = ""
    n_local: int = 0
    p25: float = float("nan")
    p75: float = float("nan")
    p90: float = float("nan")
    ci_lo: float = float("nan")
    ci_hi: float = float("nan")
    percentile: float = float("nan")
    gap_vs_med: float = float("nan")
    gap_vs_band: float = float("nan")
    sigma_log: float = float("nan")
    exact_share: float = 0.0
    median_dist_km: float = float("nan")
    competitors: int = 0
    lq: float = float("nan")
    lq_used: bool = False
    city_psqm: float = float("nan")
    reason: str = ""
    comps: object = None
    peers: int = 0


class Market:
    def __init__(self, rent_pool: pd.DataFrame, sale_pool: pd.DataFrame | None,
                 gamma: float, cfg: dict | None = None):
        self.cfg = {**DEFAULT, **(cfg or {})}
        self.cfg["gamma"] = gamma
        g = self.cfg["gamma"]
        self.other_w = self.cfg["other_w"]

        r = rent_pool[rent_pool.type.map(TYPE_W).fillna(0) > 0].copy()
        r["k"] = r.eff_g / np.power(r.area_sqm.values, g)
        r = r[np.isfinite(r.k) & (r.k > 0)]
        r["logk"] = np.log(r.k)
        self.rent = r

        # ---------- شاخص کیفیت لوکیشن از آگهی‌های فروش
        self.sale = {}
        if sale_pool is not None and len(sale_pool):
            s = sale_pool[sale_pool.psqm_sale > 0].copy()
            s["logpsqm"] = np.log(s.psqm_sale.values)
            for c, gg in s.groupby("city"):
                if len(gg) < 8: continue
                self.sale[c] = dict(tree=cKDTree(np.c_[gg.lat.values, gg.lng.values]),
                                    v=gg.logpsqm.values, lat=gg.lat.values, lng=gg.lng.values)

        # ---------- شاخص LQ برای همه آگهی‌های اجاره (برای برآورد β)
        self._lq_cache = {}
        r["lq"] = [self.lq_at(city, la, lo) for city, la, lo in zip(r.city, r.lat, r.lng)]
        self._lq_cache.clear()
        # میانگین LQ هر شهر برای مرکزکردن
        self.city_lq = {c: float(np.median(gg.lq.dropna())) for c, gg in r.groupby("city") if gg.lq.notna().sum() >= 5}
        r["dlq"] = r.apply(lambda x: x.lq - self.city_lq.get(x.city, np.nan), axis=1)
        # ---------- برآورد β با رگرسیون هابر (log k ~ ΔLQ)
        self.beta = 0.0
        self.beta_n = 0
        if self.cfg["use_lq"]:
            d = r[np.isfinite(r.dlq)]
            if len(d) >= 200:
                self.beta = self._fit_beta(d)
                self.beta_n = int(len(d))
        self.beta = float(np.clip(self.beta, 0.0, 1.5))
        # ---------- پیشین شهر روی پسماند
        r["resid"] = r.logk - self.beta * r.dlq.fillna(0.0)
        self.city_prior = {}
        for c, gg in r.groupby("city"):
            if len(gg) < 4: continue
            res = gg.resid.values
            med = float(np.median(res))
            self.city_prior[c] = dict(resid=med, n=int(len(gg)),
                                      sigma=float(1.4826 * np.median(np.abs(res - med))),
                                      city_lq=self.city_lq.get(c, np.nan))
        if self.city_prior:
            allres = r.resid.values
            self.glob = dict(resid=float(np.median(allres)), n=int(len(allres)),
                             sigma=float(1.4826 * np.median(np.abs(allres - np.median(allres)))))
        else:
            self.glob = dict(resid=0.0, n=0, sigma=0.5)

        # ---------- آرایه‌های سریع برای جست‌وجوی مکانی
        self._cities = {}
        for c, gg in r.groupby("city"):
            tw = gg.type.map(TYPE_W).values.astype(float)
            tw = np.where(gg.type.values == "commercial_other", self.other_w, tw)
            self._cities[c] = dict(
                lat=gg.lat.values, lng=gg.lng.values, area=gg.area_sqm.values,
                resid=gg.resid.values, dlq=gg.dlq.values, logk=gg.logk.values,
                k=gg.k.values, exact=gg.exact.values.astype(float),
                gk=gg.ground_known.values.astype(float), tw=tw,
                token=gg.token.values, is_comp=gg.title.fillna("").str.contains(COMP_RE).values,
                mall=gg.title.fillna("").str.contains(MALL_RE).values, df=gg)

    # ------------------------------------------------------------------ شاخص LQ
    def lq_at(self, city, lat, lng):
        key = (city, round(lat, 4), round(lng, 4))
        if key in self._lq_cache: return self._lq_cache[key]
        s = self.sale.get(city)
        if s is None:
            v = float("nan"); self._lq_cache[key] = v; return v
        for band in (self.cfg["lq_band_km"], self.cfg["lq_band_km"] * 2, self.cfg["lq_max_km"]):
            idx = s["tree"].query_ball_point([lat, lng], band / 111.0)
            if len(idx) >= 4:
                d = haversine(lat, lng, s["lat"][idx], s["lng"][idx])
                w = np.exp(-0.5 * (d / band) ** 2)
                v = wquantile(s["v"][idx], w, 0.5)
                break
        else:
            idx = s["tree"].query([lat, lng], k=min(12, len(s["v"])))[1]
            idx = np.atleast_1d(idx)
            d = haversine(lat, lng, s["lat"][idx], s["lng"][idx])
            w = np.exp(-0.5 * (d / max(np.median(d), 0.3)) ** 2)
            v = wquantile(s["v"][idx], w, 0.5)
        self._lq_cache[key] = v
        return v

    @staticmethod
    def _fit_beta(d: pd.DataFrame) -> float:
        x = d.dlq.values
        y = d.logk.values
        A = np.c_[np.ones(len(x)), x]
        b = np.linalg.lstsq(A, y, rcond=None)[0]
        for _ in range(10):
            res = y - A @ b
            s = 1.4826 * np.median(np.abs(res - np.median(res))) + 1e-9
            u = np.abs(res) / (1.5 * s)
            w = np.where(u <= 1, 1.0, 1.0 / u)
            b = np.linalg.lstsq(A * w[:, None], y * w, rcond=None)[0]
        return float(b[1])

    # ------------------------------------------------------------------ سطح محلی
    def _local(self, city, lat, lng, area, band, exclude_token=None, mall=None):
        c = self._cities.get(city)
        if c is None: return None
        d = haversine(lat, lng, c["lat"], c["lng"])
        m = d <= max(band * 2.3, 1.0)
        if exclude_token is not None: m &= (c["token"] != exclude_token)
        if m.sum() == 0: return None
        dd = d[m]
        sig = np.where(c["exact"][m] >= 1, 0.0, self.cfg["gps_sigma_km"])
        w = np.exp(-0.5 * (np.sqrt(dd ** 2 + sig ** 2) / band) ** 2)
        w *= np.exp(-0.5 * (np.log(c["area"][m] / area) / self.cfg["area_log_sd"]) ** 2)
        w *= np.where(c["gk"][m] > 0, 1.0, 0.85) * c["tw"][m]
        if mall is not None:
            same = (c["mall"][m] == mall)
            w *= np.where(same, 1.0, 0.45)
        if w.sum() <= 0: return None
        resid = c["resid"][m]
        med, mad = wmedian_mad(resid, w)
        effn = float(w.sum() ** 2 / (w ** 2).sum())
        k = c["k"][m]
        return dict(resid=med, effn=effn, mad=mad if np.isfinite(mad) else self.glob["sigma"],
                    p25=wquantile(k, w, .25), p50=wquantile(k, w, .5),
                    p75=wquantile(k, w, .75), p90=wquantile(k, w, .90),
                    exact=float(c["exact"][m].mean()), mdist=float(np.median(dd)), n=int(m.sum()),
                    competitors=int(np.sum((dd <= 2.0) & c["is_comp"][m])),
                    peers=(int(np.sum((dd <= 6.0) & (c["mall"][m] == mall))) if mall is not None else 0),
                    dist=dd, w=w, k=k)

    # ------------------------------------------------------------------ تخمین
    def estimate(self, lat, lng, area, city, exclude_token=None, cfg=None,
                 actual_cost_ground=None, mall=None) -> Est:
        if cfg: self.cfg.update(cfg)
        c = self.cfg
        g = c["gamma"]
        if not (area and area > 0): return Est(ok=False, reason="متراژ نامعتبر")
        cp = self.city_prior.get(city)
        if cp is None: return Est(ok=False, reason="این شهر در دیتاست نیست")
        lq = self.lq_at(city, lat, lng)
        city_lq = self.city_lq.get(city, np.nan)
        dlq = lq - city_lq if (np.isfinite(lq) and np.isfinite(city_lq)) else 0.0
        lq_used = bool(self.cfg["use_lq"] and np.isfinite(dlq) and abs(dlq) > 1e-9)
        lq_term = self.beta * dlq if lq_used else 0.0

        loc, band = None, c["band_km"]
        for band in (c["band_km"], c["band_km"] * 2, c["band_max_km"]):
            loc = self._local(city, lat, lng, area, band, exclude_token, mall)
            if loc and loc["effn"] >= 8: break

        if loc:
            wl = loc["effn"] / (loc["effn"] + c["shrink_k"])
            # پسماند محلی نسبت به پیشین شهر (نه سطح مطلق!) — جلوگیری از دوباره‌شماری
            loc_dev = loc["resid"] - cp["resid"]
            logk = cp["resid"] + lq_term + wl * loc_dev
            sigma = math.sqrt(wl ** 2 * loc["mad"] ** 2 / max(loc["effn"], 1) + (1 - wl) ** 2 * cp["sigma"] ** 2)
            se = max(0.05, 1.2533 * max(loc["mad"], cp["sigma"]) * math.sqrt(1 / max(loc["effn"], 1) + 1 / max(cp["n"], 1)))
            level = "local" if wl >= 0.6 else "hybrid"
        else:
            logk = cp["resid"] + lq_term
            sigma = cp["sigma"]
            se = max(0.12, max(cp["sigma"], .25) * math.sqrt(1 / 12 + 1 / max(cp["n"], 1)))
            level = "city"

        fair = math.exp(logk) * area ** g
        pct = gap_med = gap_band = float("nan")
        if actual_cost_ground and actual_cost_ground > 0 and loc:
            k_act = math.log(actual_cost_ground / area ** g)
            pct = float(loc["w"][loc["k"] <= math.exp(k_act)].sum() / max(loc["w"].sum(), 1e-9))
            gap_med = actual_cost_ground / fair - 1
            p75_cost = loc["p75"] * area ** g
            gap_band = actual_cost_ground / p75_cost - 1 if p75_cost > 0 else float("nan")
        elif actual_cost_ground and actual_cost_ground > 0:
            gap_med = actual_cost_ground / fair - 1
        return Est(
            ok=True, fair_cost=fair, fair_psqm=fair / area, effN=(loc["effn"] if loc else 0.0),
            level=level, n_local=(loc["n"] if loc else 0),
            p25=(loc["p25"] * area ** g if loc else fair),
            p75=(loc["p75"] * area ** g if loc else fair),
            p90=(loc["p90"] * area ** g if loc else fair),
            ci_lo=fair * math.exp(-1.96 * se), ci_hi=fair * math.exp(1.96 * se),
            percentile=pct, gap_vs_med=gap_med, gap_vs_band=gap_band, sigma_log=float(sigma),
            exact_share=(loc["exact"] if loc else 0.0),
            median_dist_km=(loc["mdist"] if loc else float("nan")),
            competitors=(loc["competitors"] if loc else 0),
            peers=(loc["peers"] if loc else 0),
            lq=float(lq) if np.isfinite(lq) else float("nan"), lq_used=lq_used,
            city_psqm=(math.exp(cp["resid"] + self.beta * 0) * (1.0) if False else float("nan")),
            comps=loc)

    # ------------------------------------------------------------------ مشابه‌ها (نمایش)
    def comparables(self, lat, lng, area, city, radius_km=5.0):
        c = self._cities.get(city)
        if c is None: return pd.DataFrame()
        d = haversine(lat, lng, c["lat"], c["lng"])
        m = d <= radius_km
        if m.sum() == 0: return pd.DataFrame()
        sub = c["df"].iloc[np.where(m)[0]].copy()
        sub["dist"] = d[m]
        sub["w"] = (np.exp(-0.5 * (d[m] / self.cfg["band_km"]) ** 2)
                    * np.exp(-0.5 * (np.log(sub.area_sqm.values / area) / self.cfg["area_log_sd"]) ** 2))
        return sub.sort_values("w", ascending=False)
