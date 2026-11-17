"""
live_demo.py — a fake Divar API for offline demos and tests.

`install_fake_divar(stores)` plugs an httpx.MockTransport into divar-mcp, so the real collector code
(divar_live.py) runs end-to-end without touching the network. It answers the same endpoints the
collector uses, in the same JSON shape as api.divar.ir:

    GET  /v8/places/find?lat=&long=          city of a point
    GET  /v8/places/cities                   city list with centroids
    GET  /v8/places/cities/{id}/districts    neighbourhoods with centroids
    POST /v8/mapview/viewport                map pins in a bbox (max 200, true count in `count`)
    POST /v8/postlist/w/search               list rows (used for ads without a map pin)
    GET  /v8/posts-v2/web/{token}            one ad page

All listings are SYNTHETIC (titles start with «[نمایشی]», tokens with DEMO). Prices are drawn around
each store's own rent per m² so the comparison screens look realistic — never use them for decisions.
"""
from __future__ import annotations

import json
import math
import random
import re
from urllib.parse import parse_qs, urlparse

import httpx

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
CITY_IDS = {"تهران": 1, "کرج": 2, "مشهد": 3, "اصفهان": 4, "شیراز": 6, "اهواز": 7, "قم": 8, "ارومیه": 10,
            "کرمان": 13, "بندرعباس": 18, "بابلسر": 710, "سیرجان": 818, "کلارآباد": 1695, "لواسان": 1771}


def fa(n: int) -> str:
    return f"{n:,}".replace(",", "٬").translate(FA_DIGITS)


def short(n: int) -> str:
    """'۱٫۵ میلیارد' / '۲۵۰ میلیون' like Divar's map chips."""
    if n >= 1_000_000_000:
        v = round(n / 1e9, 1)
        return (f"{v:g}".replace(".", "٫")).translate(FA_DIGITS) + " میلیارد"
    if n >= 1_000_000:
        return str(round(n / 1e6)).translate(FA_DIGITS) + " میلیون"
    return fa(n)


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class FakeDivar:
    def __init__(self, stores: list[dict], seed: int = 7):
        rnd = random.Random(seed)
        self.ads: dict[str, dict] = {}
        self.cities: dict[int, dict] = {}
        self.districts: dict[int, list[dict]] = {}
        n = 0
        for s in stores:
            cname = s.get("city") or "تهران"
            cid = CITY_IDS.get(cname, 9000 + len(self.cities))
            c = self.cities.setdefault(cid, {"id": cid, "name": cname, "lat": [], "lng": []})
            c["lat"].append(s["lat"]); c["lng"].append(s["lng"])
            nb = (s.get("neighborhood_or_street") or s.get("store_name") or "مرکز").strip()
            dl = self.districts.setdefault(cid, [])
            did = cid * 1000 + len(dl) + 1
            dl.append({"id": did, "name": nb, "lat": s["lat"], "lon": s["lng"]})
            area = _num(s.get("area_sqm")) or 120
            rent = _num(s.get("monthly_rent_toman")) or _num(s.get("minimum_rent_toman")) or 80_000_000
            base_psqm = max(rent / area, 150_000)
            for _ in range(rnd.randint(90, 260)):
                n += 1
                km = abs(rnd.gauss(0, 2.6))
                ang = rnd.uniform(0, 2 * math.pi)
                lat = s["lat"] + km * math.cos(ang) / 111.32
                lng = s["lng"] + km * math.sin(ang) / (111.32 * math.cos(math.radians(s["lat"])))
                slug = "shop-rent" if rnd.random() < 0.8 else "office-rent"
                a = int(max(12, rnd.lognormvariate(math.log(area * 0.6), 0.7)))
                psqm = base_psqm * rnd.lognormvariate(0, 0.35) * (1.15 if km < 1 else 1.0)
                eq = psqm * a
                split = rnd.random()
                if split < 0.12:                         # full deposit (رهن کامل)
                    dep, r = int(round(eq / 0.03, -7)), 0
                else:
                    dep = int(round(eq * rnd.uniform(3, 15), -6))
                    r = int(round(max(eq - dep * 0.03, eq * 0.25), -5))
                tok = f"DEMO{n:06d}"
                self.ads[tok] = {
                    "token": tok, "slug": slug, "city_id": cid, "city": cname, "district": nb, "district_id": did,
                    "lat": lat, "lng": lng, "area": a, "deposit": dep, "rent": r,
                    "approx": rnd.random() < 0.3, "pinned": rnd.random() < 0.85,
                    "floor": rnd.choice(["همکف", "همکف", "همکف", "۱", "زیرهمکف", "۲"]),
                    "title": f"[نمایشی] {'مغازه' if slug == 'shop-rent' else 'دفتر کار'} {a} متری {nb}",
                }
        for c in self.cities.values():
            c["clat"] = sum(c["lat"]) / len(c["lat"]); c["clng"] = sum(c["lng"]) / len(c["lng"])

    # -- endpoint handlers
    def city_of(self, lat, lng):
        best = min(self.cities.values(), key=lambda c: (c["clat"] - lat) ** 2 + (c["clng"] - lng) ** 2)
        return best

    def handle(self, request: httpx.Request) -> httpx.Response:
        u = urlparse(str(request.url))
        path = u.path.removeprefix("/v8")
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        if path == "/places/find":
            q = parse_qs(u.query)
            c = self.city_of(float(q["lat"][0]), float(q["long"][0]))
            return httpx.Response(200, json={"city": {"id": c["id"], "name": c["name"]}, "display_text": c["name"]})
        if path == "/places/cities":
            return httpx.Response(200, json={"cities": [
                {"id": c["id"], "name": c["name"], "slug": str(c["id"]),
                 "centroid": {"latitude": c["clat"], "longitude": c["clng"]}} for c in self.cities.values()]})
        m = re.fullmatch(r"/places/cities/(\d+)/districts", path)
        if m:
            return httpx.Response(200, json={"districts": [
                {"id": d["id"], "name": d["name"], "slug": str(d["id"]),
                 "centroid": {"latitude": d["lat"], "longitude": d["lon"]}}
                for d in self.districts.get(int(m.group(1)), [])]})
        if path == "/mapview/viewport":
            return self.viewport(body)
        if path == "/postlist/w/search":
            return self.postlist(body)
        m = re.fullmatch(r"/posts-v2/web/([A-Za-z0-9_-]+)", path)
        if m:
            return self.post(m.group(1))
        return httpx.Response(403, json={"message": "RBAC: access denied"})

    @staticmethod
    def _slug(body):
        return (((body.get("search_data") or {}).get("form_data") or {}).get("data") or {}).get(
            "category", {}).get("str", {}).get("value")

    def viewport(self, body):
        b = body["camera_info"]["bbox"]
        slug = self._slug(body)
        cids = {int(c) for c in body.get("city_ids") or []}
        hits = [a for a in self.ads.values() if a["pinned"] and a["slug"] == slug and a["city_id"] in cids
                and b["min_latitude"] <= a["lat"] < b["max_latitude"]
                and b["min_longitude"] <= a["lng"] < b["max_longitude"]]
        posts = [{
            "map_post_card": {"token": a["token"], "title": a["title"],
                              "price_fields": [{"title": "ودیعه:", "value": short(a["deposit"])},
                                               {"title": "اجاره:", "value": short(a["rent"]) if a["rent"] else "رایگان"}],
                              "chips": [{"title": f"{fa(a['area'])} متر"}]},
            "map_pin_feature": {"lat": a["lat"], "lon": a["lng"], "approximate_location": a["approx"]},
        } for a in hits[:200]]
        return httpx.Response(200, json={"count": len(hits), "posts": posts})

    def postlist(self, body):
        slug = self._slug(body)
        data = body["search_data"]["form_data"]["data"]
        dids = set((data.get("districts") or {}).get("repeated_string", {}).get("value") or [])
        rows = []
        for a in self.ads.values():
            if a["slug"] != slug or (dids and str(a["district_id"]) not in dids):
                continue
            rows.append({"widget_type": "POST_ROW", "data": {
                "token": a["token"], "title": a["title"],
                "top_description_text": f"ودیعه: {fa(a['deposit'])} تومان",
                "middle_description_text": f"اجاره: {fa(a['rent'])} تومان" if a["rent"] else "رهن کامل",
                "bottom_description_text": f"لحظاتی پیش در {a['district']}",
                "action": {"payload": {"web_info": {"district_persian": a["district"]}}}}})
        return httpx.Response(200, json={"list_widgets": rows[:25], "pagination": {"has_next_page": False}})

    def post(self, token):
        a = self.ads.get(token)
        if not a:
            return httpx.Response(404, json={"message": "post not found"})
        loc = {"type": "FUZZY" if a["approx"] else "EXACT"}
        loc["fuzzy_data" if a["approx"] else "exact_data"] = {"point": {"latitude": a["lat"], "longitude": a["lng"]}}
        rows = [{"widget_type": "GROUP_INFO_ROW", "data": {"items": [
                    {"title": "متراژ", "value": fa(a["area"])}, {"title": "ساخت", "value": "۱۳۹۵"}]}},
                {"widget_type": "UNEXPANDABLE_ROW", "data": {"title": "ودیعه", "value": f"{fa(a['deposit'])} تومان"}},
                {"widget_type": "UNEXPANDABLE_ROW", "data": {"title": "طبقه", "value": a["floor"]}}]
        if a["rent"]:
            rows.append({"widget_type": "UNEXPANDABLE_ROW",
                         "data": {"title": "اجارهٔ ماهانه", "value": f"{fa(a['rent'])} تومان"}})
        return httpx.Response(200, json={
            "sections": [
                {"section_name": "TITLE", "widgets": [{"widget_type": "LEGEND_TITLE_ROW", "data": {"title": a["title"]}}]},
                {"section_name": "LIST_DATA", "widgets": rows},
                {"section_name": "MAP", "widgets": [{"widget_type": "MAP_ROW", "data": {"location": loc}}]},
            ],
            "webengage": {"credit": a["deposit"], "rent": a["rent"], "category": a["slug"]},
            "seo": {"web_info": {"district_persian": a["district"]}},
            "city": {"name": a["city"], "city_id": str(a["city_id"])},
        })


def install_fake_divar(stores: list[dict], seed: int = 7) -> FakeDivar:
    from divar_mcp import http as dhttp
    fake = FakeDivar(stores, seed)
    dhttp.set_transport(httpx.MockTransport(fake.handle))
    dhttp.JITTER = (0.0, 0.0)        # no politeness delay needed against the fake
    return fake
