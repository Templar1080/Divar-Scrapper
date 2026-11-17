"""
divar_live.py — collect every commercial rent listing on Divar (with real map coordinates,
deposit and monthly rent) through the divar-mcp package, and write it in the format
real_estate_map.html already understands.

How it works
    1. Areas  = a circle around every store in the stores CSV(s) (+ optional extra cities).
    2. Map    = each area is covered by ~2 km tiles; every tile is queried on Divar's map view
                (the same call dv_nearby uses). A tile that holds more ads than the map returns
                (≈200) is split into 4 smaller tiles, recursively — so dense areas are not cut off.
    3. Unpinned (optional) = ads without a map pin are pulled from the neighbourhood list search
                and placed on the neighbourhood centroid (flagged as estimated).
    4. Details = every new ad is opened once (dv_post) for the exact deposit, rent, area, floor,
                neighbourhood and location; details are cached in SQLite and refreshed every N days.
    5. Export  = live_data/data.json          (listings for the map, same schema as divar_scraper)
                 live_data/history.json       (daily neighbourhood medians, for the trend chart)
                 live_data/store_comparison.csv (every store vs. the listings around it)

Usage
    pip install -r requirements-live.txt
    python divar_live.py run                              # stores from myli_stores.csv, 6 km around each
    python divar_live.py run --radius-km 8 --city "تهران:18"
    python divar_live.py run --demo                       # offline demo with synthetic listings
    python divar_live.py export                           # rebuild the JSON/CSV from the local DB
    python live_map_server.py                             # map + automatic refresh (see that file)

Prices are Toman everywhere (deposit, monthly_rent). price_total for rent rows follows the
divar_scraper convention: monthly equivalent in *thousand* Toman (rent + deposit × 3%) / 1000.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import re
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "live_data"
DB_PATH = OUT_DIR / "divar_live.db"
DATA_JSON = OUT_DIR / "data.json"
HISTORY_JSON = OUT_DIR / "history.json"
COMPARISON_CSV = OUT_DIR / "store_comparison.csv"
LAST_RUN_JSON = OUT_DIR / "last_run.json"
DEMO_DIR = ROOT / "live_data_demo"      # demo data never mixes with real data

DEFAULT_STORES = [ROOT / "myli_stores.csv", ROOT / "hyperstar_stores.csv"]
DEFAULT_CATEGORIES = ["shop-rent", "office-rent"]   # Divar slugs: مغازه / دفتر کار (اجاره)
RAHN_MONTHLY_RATE = 0.03      # price_total convention of divar_scraper.py
MAP_RAHN = 0.025              # RAHN used inside real_estate_map.html (history + comparison CSV)
MAP_CAP = 200                 # Divar's map view returns at most ~200 pins per viewport
LAT_REF = 33.0                # fixed reference latitude so the tile grid is global (dedupe across stores)
KM_PER_DEG = 111.32

log = logging.getLogger("divar_live")
Progress = Callable[[str, int, int, str], None]


# ---------------------------------------------------------------- config
@dataclass
class LiveConfig:
    stores_files: list[Path] = field(default_factory=lambda: list(DEFAULT_STORES))
    radius_km: float = 6.0                 # = the first comparison radius used by the map
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    extra_cities: list[tuple[str, float]] = field(default_factory=list)   # [("تهران", 18.0)]
    tile_km: float = 2.0
    max_depth: int = 4                     # 2 km → 125 m tiles at most
    include_unpinned: bool = True
    unpinned_pages: int = 2                # list pages (~25 ads) per neighbourhood and category
    max_details: int = 1500                # ad pages opened per run (rest continue next run)
    detail_refresh_days: int = 14
    stale_days: int = 3                    # an ad not seen on the map for this long is dropped
    concurrency: int = 4
    out_dir: Path = OUT_DIR

    @property
    def db_path(self) -> Path:
        return self.out_dir / "divar_live.db"


class Aborted(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers
_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
AREA_CHIP = re.compile(r"^(\d{1,5})\s*متر")
TITLE_AREA = re.compile(r"(\d{2,4})\s*(?:متری|متر|m(?![a-z]))")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v).translate(_FA).replace(",", "").replace("٬", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return statistics.median(xs) if xs else None


# ---------------------------------------------------------------- stores
def read_stores(paths: list[Path]) -> list[dict]:
    """Stores with coordinates from one or more CSV/JSON files (map template format)."""
    out = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8-sig", errors="ignore").strip()
        if not text:
            continue
        if text[0] in "[{":
            js = json.loads(text)
            rows = js if isinstance(js, list) else (js.get("stores") or js.get("items") or [])
        else:
            rows = list(csv.DictReader(text.splitlines()))
        brand = p.stem.split("_")[0]
        for r in rows:
            lat, lng = num(r.get("lat")), num(r.get("lng"))
            if (lat is None or lng is None) and r.get("coordinates"):
                parts = re.split(r"[,،]", str(r["coordinates"]))
                if len(parts) >= 2:
                    lat, lng = num(parts[0]), num(parts[1])
            if lat is None or lng is None or not (24 <= lat <= 40 and 43 <= lng <= 64):
                continue
            out.append({**r, "brand": brand, "lat": lat, "lng": lng,
                        "store_name": r.get("store_name") or r.get("name") or "",
                        "city": (r.get("city") or "").strip()})
    return out


# ---------------------------------------------------------------- tiles
@dataclass
class Area:
    name: str
    lat: float
    lng: float
    radius_km: float
    city_hint: str = ""


def _steps(tile_km: float) -> tuple[float, float]:
    return tile_km / KM_PER_DEG, tile_km / (KM_PER_DEG * math.cos(math.radians(LAT_REF)))


def root_tiles(area: Area, tile_km: float) -> list[tuple[int, int]]:
    """Grid cells (global grid) whose rectangle touches the area's circle."""
    s_lat, s_lng = _steps(tile_km)
    r_lat = area.radius_km / KM_PER_DEG
    r_lng = area.radius_km / (KM_PER_DEG * max(math.cos(math.radians(area.lat)), 0.2))
    cells = []
    for i in range(math.floor((area.lat - r_lat) / s_lat), math.floor((area.lat + r_lat) / s_lat) + 1):
        for j in range(math.floor((area.lng - r_lng) / s_lng), math.floor((area.lng + r_lng) / s_lng) + 1):
            lo_lat, lo_lng = i * s_lat, j * s_lng
            near_lat = min(max(area.lat, lo_lat), lo_lat + s_lat)
            near_lng = min(max(area.lng, lo_lng), lo_lng + s_lng)
            if haversine_km(area.lat, area.lng, near_lat, near_lng) <= area.radius_km:
                cells.append((i, j))
    return cells


def cell_bbox(cell: tuple[int, int], tile_km: float) -> tuple[float, float, float, float]:
    s_lat, s_lng = _steps(tile_km)
    i, j = cell
    return i * s_lat, j * s_lng, (i + 1) * s_lat, (j + 1) * s_lng   # min_lat, min_lng, max_lat, max_lng


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS listings(
  token TEXT PRIMARY KEY, category_slug TEXT, city_id TEXT, city_name TEXT, title TEXT,
  pin_lat REAL, pin_lng REAL, pin_approx INTEGER, pin_deposit REAL, pin_rent REAL, pin_area REAL,
  district TEXT, on_map INTEGER DEFAULT 1,
  first_seen TEXT, last_seen TEXT, detail_json TEXT, detail_at TEXT, gone INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_seen ON listings(last_seen);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def upsert_seen(conn, row: dict, seen_at: str) -> bool:
    """Insert or refresh an ad seen in this run. Returns True when the ad is new."""
    cur = conn.execute("SELECT on_map FROM listings WHERE token=?", (row["token"],)).fetchone()
    if cur is None:
        conn.execute(
            "INSERT INTO listings(token,category_slug,city_id,city_name,title,pin_lat,pin_lng,pin_approx,"
            "pin_deposit,pin_rent,pin_area,district,on_map,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["token"], row["category_slug"], row["city_id"], row["city_name"], row["title"],
             row["lat"], row["lng"], int(bool(row["approx"])), row["deposit"], row["rent"], row["area"],
             row.get("district") or "", int(row["on_map"]), seen_at, seen_at))
        return True
    if row["on_map"] or not cur[0]:
        # a real map pin always wins over a neighbourhood-centroid placement
        conn.execute(
            "UPDATE listings SET category_slug=?,city_id=?,city_name=?,title=?,pin_lat=?,pin_lng=?,pin_approx=?,"
            "pin_deposit=COALESCE(?,pin_deposit),pin_rent=COALESCE(?,pin_rent),pin_area=COALESCE(?,pin_area),"
            "district=CASE WHEN ?<>'' THEN ? ELSE district END,on_map=?,last_seen=?,gone=0 WHERE token=?",
            (row["category_slug"], row["city_id"], row["city_name"], row["title"], row["lat"], row["lng"],
             int(bool(row["approx"])), row["deposit"], row["rent"], row["area"],
             row.get("district") or "", row.get("district") or "", int(row["on_map"]), seen_at, row["token"]))
    else:
        conn.execute("UPDATE listings SET last_seen=?,gone=0 WHERE token=?", (seen_at, row["token"]))
    return False


# ---------------------------------------------------------------- divar access (via divar-mcp)
def _mcp():
    try:
        from divar_mcp import http as dhttp
        from divar_mcp.core import Q, district_list, fetch_cards, fold
        from divar_mcp.post import parse_post
        from divar_mcp.search import pin
    except ImportError as e:  # pragma: no cover
        raise SystemExit("divar-mcp is not installed: pip install -r requirements-live.txt") from e
    return dhttp, Q, district_list, fetch_cards, fold, parse_post, pin


def _category_data(slug: str) -> dict:
    return {"category": {"str": {"value": slug}}}


class Collector:
    def __init__(self, cfg: LiveConfig, progress: Progress | None = None):
        self.cfg = cfg
        self.progress = progress or (lambda phase, done, total, msg="": None)
        (self.dhttp, self.Q, self.district_list, self.fetch_cards,
         self.fold, self.parse_post, self.pin) = _mcp()
        self.errors: list[str] = []
        self._consecutive_fail = 0
        self.stats: dict[str, Any] = {}

    # -- error bookkeeping: stop early when Divar blocks us instead of hammering it
    def _ok(self):
        self._consecutive_fail = 0

    def _fail(self, what: str, e: Exception):
        self._consecutive_fail += 1
        msg = f"{what}: {e}"
        if len(self.errors) < 50:
            self.errors.append(msg)
        log.warning(msg)
        if self._consecutive_fail >= 25:
            raise Aborted(f"Divar failed 25 times in a row — last error: {e}")

    async def api(self, path: str, body: dict | None = None) -> dict:
        return await self.dhttp.api(path, body=body)

    # -- places
    async def place(self, lat: float, lng: float) -> tuple[str, str]:
        d = await self.api(f"/places/find?lat={lat:.5f}&long={lng:.5f}")
        c = d.get("city") or {}
        return str(c.get("id") or ""), c.get("name") or c.get("title") or ""

    async def city_centroid(self, name: str) -> tuple[float, float, str] | None:
        d = await self.api("/places/cities")
        for c in d.get("cities") or []:
            if self.fold(c.get("name", "")) == self.fold(name):
                ce = c.get("centroid") or {}
                if ce.get("latitude"):
                    return ce["latitude"], ce["longitude"], str(c["id"])
        return None

    # -- map tiles
    async def fetch_tile(self, city_ids: list[str], slug: str, bbox, depth: int, sem: asyncio.Semaphore) -> list[dict]:
        min_lat, min_lng, max_lat, max_lng = bbox
        body = {
            "search_data": {"form_data": {"data": _category_data(slug)}},
            "camera_info": {"bbox": {"min_latitude": min_lat, "min_longitude": min_lng,
                                     "max_latitude": max_lat, "max_longitude": max_lng},
                            "zoom": min(14 + depth, 18)},
            "city_ids": city_ids,        # every city touching the tile, so border areas are not cut off
        }
        async with sem:
            try:
                data = await self.api("/mapview/viewport", body)
                self._ok()
            except Aborted:
                raise
            except Exception as e:  # noqa: BLE001
                self._fail(f"map tile {slug} {bbox[0]:.4f},{bbox[1]:.4f}", e)
                return []
        posts = data.get("posts") or []
        count = int(data.get("count") or len(posts))
        if (count > len(posts) or len(posts) >= MAP_CAP) and depth < self.cfg.max_depth:
            mid_lat, mid_lng = (min_lat + max_lat) / 2, (min_lng + max_lng) / 2
            quads = [(min_lat, min_lng, mid_lat, mid_lng), (min_lat, mid_lng, mid_lat, max_lng),
                     (mid_lat, min_lng, max_lat, mid_lng), (mid_lat, mid_lng, max_lat, max_lng)]
            parts = await asyncio.gather(*(self.fetch_tile(city_ids, slug, q, depth + 1, sem) for q in quads))
            return [p for part in parts for p in part]
        if count > len(posts):
            self.stats["truncated_tiles"] = self.stats.get("truncated_tiles", 0) + 1
        return posts

    def parse_pin(self, p: dict, slug: str, city_id: str, city_name: str) -> dict | None:
        out = self.pin(p, 0.0, 0.0)
        if not out.get("token") or not out.get("lat"):
            return None
        area = None
        for chip in out.get("chips") or []:
            m = AREA_CHIP.match(self.fold(chip))
            if m:
                area = float(m.group(1))
                break
        if area is None:
            m = TITLE_AREA.search(self.fold(out.get("title", "")))
            area = float(m.group(1)) if m else None
        return {"token": out["token"], "category_slug": slug, "city_id": city_id, "city_name": city_name,
                "title": out.get("title", ""), "lat": out["lat"], "lng": out["lon"],
                "approx": bool(out.get("approximate_location")), "deposit": out.get("deposit"),
                "rent": out.get("rent"), "area": area, "on_map": True}

    # -- unpinned ads: neighbourhood list search, placed on the neighbourhood centroid
    async def unpinned(self, city_id: str, city_name: str, centres: list[Area], slug: str,
                       sem: asyncio.Semaphore) -> list[dict]:
        try:
            districts = await self.district_list(int(city_id))
            self._ok()
        except Aborted:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(f"districts of {city_name}", e)
            return []
        near = [d for d in districts if d.get("lat") and any(
            haversine_km(a.lat, a.lng, d["lat"], d["lon"]) <= a.radius_km for a in centres)]

        async def one(d):
            q = self.Q([{"id": int(city_id), "name": city_name}], slug, "",
                       {**_category_data(slug), "districts": {"repeated_string": {"value": [str(d["id"])]}}})
            async with sem:
                try:
                    got = await self.fetch_cards(q, pages=self.cfg.unpinned_pages)
                    self._ok()
                except Aborted:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._fail(f"list {slug} {d.get('name')}", e)
                    return []
            rows = []
            for c in got.get("cards") or []:
                if not c.get("token"):
                    continue
                m = TITLE_AREA.search(self.fold(c.get("title", "")))
                rows.append({"token": c["token"], "category_slug": slug, "city_id": city_id,
                             "city_name": city_name, "title": c.get("title", ""),
                             "lat": d["lat"], "lng": d["lon"], "approx": True,
                             "deposit": c.get("deposit"), "rent": c.get("rent"),
                             "area": float(m.group(1)) if m else None,
                             "district": c.get("district") or d.get("name") or "", "on_map": False})
            return rows

        parts = await asyncio.gather(*(one(d) for d in near))
        return [r for part in parts for r in part]

    # -- details
    async def fetch_detail(self, token: str, sem: asyncio.Semaphore) -> tuple[str, dict | None, bool]:
        async with sem:
            try:
                data = await self.api(f"/posts-v2/web/{token}")
                self._ok()
                return token, self.parse_post(token, data, max_images=0), False
            except Aborted:
                raise
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status", None)
                if status in (404, 410):
                    self._ok()
                    return token, None, True
                self._fail(f"ad {token}", e)
                return token, None, False

    # -- main
    async def run(self) -> dict:
        cfg = self.cfg
        self.dhttp.set_transport(getattr(self.dhttp, "_transport", None))   # fresh client for this event loop
        started = now_utc()
        seen_at = iso(started)
        conn = open_db(cfg.db_path)
        sem = asyncio.Semaphore(cfg.concurrency)
        try:
            # 1) areas
            stores = read_stores(cfg.stores_files)
            areas = [Area(s["store_name"] or "store", s["lat"], s["lng"], cfg.radius_km, s["city"]) for s in stores]
            for name, radius in cfg.extra_cities:
                c = await self.city_centroid(name)
                if c:
                    areas.append(Area(name, c[0], c[1], radius, name))
                else:
                    self.errors.append(f"city not found on Divar: {name}")
            if not areas:
                raise Aborted("No areas: the stores file has no coordinates and no --city was given.")
            self.progress("areas", 0, len(areas), f"{len(areas)} محدوده")

            # 2) city of every area + global tile set
            tiles: dict[tuple[int, int], dict[str, str]] = {}
            area_city: list[tuple[Area, str, str]] = []
            place_cache: dict[tuple[float, float], tuple[str, str]] = {}
            for k, a in enumerate(areas):
                key = (round(a.lat, 2), round(a.lng, 2))
                if key not in place_cache:
                    try:
                        place_cache[key] = await self.place(a.lat, a.lng)
                        self._ok()
                    except Aborted:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self._fail(f"locate {a.name}", e)
                        place_cache[key] = ("", a.city_hint)
                city_id, city_name = place_cache[key]
                if not city_id:
                    continue
                city_name = city_name or a.city_hint
                area_city.append((a, city_id, city_name))
                for cell in root_tiles(a, cfg.tile_km):
                    tiles.setdefault(cell, {})[city_id] = city_name
                self.progress("areas", k + 1, len(areas), a.name)
            if not area_city:
                raise Aborted("Could not locate any store on Divar — is divar.ir reachable from this machine? "
                              + (self.errors[-1] if self.errors else ""))
            self.stats["areas"] = len(area_city)
            self.stats["root_tiles"] = len(tiles)

            # 3) map tiles
            found: dict[str, dict] = {}
            jobs = [(cell, cities, slug) for cell, cities in tiles.items() for slug in cfg.categories]
            done = 0

            def city_of(lat, lng, cities: dict[str, str]) -> tuple[str, str]:
                """Tile spans several cities → the city of the nearest store area (the ad page corrects it later)."""
                if len(cities) == 1:
                    return next(iter(cities.items()))
                a, cid, cname = min(((a, cid, cn) for a, cid, cn in area_city if cid in cities),
                                    key=lambda t: haversine_km(lat, lng, t[0].lat, t[0].lng))
                return cid, cname

            async def tile_job(cell, cities, slug):
                nonlocal done
                posts = await self.fetch_tile(sorted(cities), slug, cell_bbox(cell, cfg.tile_km), 0, sem)
                for p in posts:
                    row = self.parse_pin(p, slug, "", "")
                    if row:
                        row["city_id"], row["city_name"] = city_of(row["lat"], row["lng"], cities)
                        found.setdefault(row["token"], row)
                done += 1
                self.progress("map", done, len(jobs), f"{len(found)} آگهی روی نقشه")

            await asyncio.gather(*(tile_job(*j) for j in jobs))
            self.stats["map_ads"] = len(found)

            # 4) unpinned ads
            if cfg.include_unpinned:
                by_city: dict[tuple[str, str], list[Area]] = {}
                for a, cid, cname in area_city:
                    by_city.setdefault((cid, cname), []).append(a)
                ujobs = [(cid, cname, al, slug) for (cid, cname), al in by_city.items() for slug in cfg.categories]
                extra = 0
                for k, (cid, cname, al, slug) in enumerate(ujobs):
                    for row in await self.unpinned(cid, cname, al, slug, sem):
                        if row["token"] not in found:
                            found[row["token"]] = row
                            extra += 1
                    self.progress("list", k + 1, len(ujobs), f"+{extra} آگهی بدون پین")
                self.stats["unpinned_ads"] = extra

            new = sum(upsert_seen(conn, r, seen_at) for r in found.values())
            conn.commit()
            self.stats["new_ads"] = new

            # 5) details (new first, then the oldest refresh)
            cutoff_seen = iso(started - timedelta(days=cfg.stale_days))
            cutoff_detail = iso(started - timedelta(days=cfg.detail_refresh_days))
            cand = conn.execute(
                "SELECT token, detail_at IS NOT NULL, pin_lat, pin_lng FROM listings "
                "WHERE gone=0 AND last_seen>=? AND (detail_at IS NULL OR detail_at<?)",
                (cutoff_seen, cutoff_detail)).fetchall()
            pts = [(s["lat"], s["lng"]) for s in stores]

            def nearest_store_km(lat, lng):
                return min((haversine_km(lat, lng, a, b) for a, b in pts), default=0.0) if lat else 1e9

            # never-opened ads first, and among them the ones closest to a store (that is where the comparison is)
            cand.sort(key=lambda r: (r[1], nearest_store_km(r[2], r[3])))
            todo = [r[0] for r in cand[:cfg.max_details]]
            pending_total = len(cand)
            done = 0
            gone = 0
            for i in range(0, len(todo), 40):          # commit in small batches: progress survives a crash
                batch = await asyncio.gather(*(self.fetch_detail(t, sem) for t in todo[i:i + 40]))
                stamp = iso(now_utc())
                for token, detail, is_gone in batch:
                    if is_gone:
                        conn.execute("UPDATE listings SET gone=1 WHERE token=?", (token,))
                        gone += 1
                    elif detail is not None:
                        conn.execute("UPDATE listings SET detail_json=?,detail_at=? WHERE token=?",
                                     (json.dumps(detail, ensure_ascii=False), stamp, token))
                conn.commit()
                done += len(batch)
                self.progress("details", done, len(todo), f"{done} از {len(todo)} آگهی باز شد")
            self.stats.update(details_fetched=done, details_pending=max(pending_total - done, 0), removed_ads=gone)
        finally:
            # 6) export — even after an abort, so the map keeps what was collected
            try:
                self.stats.update(export_all(cfg, conn))
            finally:
                conn.close()
        return self.stats


# ---------------------------------------------------------------- export
def build_record(row: sqlite3.Row) -> dict | None:
    d = json.loads(row["detail_json"]) if row["detail_json"] else None
    lat = (d or {}).get("lat") or row["pin_lat"]
    lng = (d or {}).get("lon") or row["pin_lng"]
    if lat is None or lng is None:
        return None
    if d:
        approx = d.get("approximate_location")
        if d.get("lat") is None:                  # the ad page has no point → we only know the centroid / pin
            approx = True if not row["on_map"] else row["pin_approx"]
        deposit, rent = d.get("deposit"), d.get("rent")
        full = bool(d.get("full_deposit"))
        area = d.get("area") or row["pin_area"]
        floor = (d.get("specs") or {}).get("طبقه")
        nb = d.get("district") or row["district"] or ""
        city = d.get("city") or row["city_name"] or ""
        when = (d.get("bumped_at") or d.get("published_at") or row["first_seen"] or "")[:10]
        title = d.get("title") or row["title"]
        suspect = d.get("suspect_price") or None
        src = "detail"
    else:
        approx = row["pin_approx"] or not row["on_map"]
        deposit, rent = row["pin_deposit"], row["pin_rent"]
        full = bool(deposit) and rent == 0
        area, floor, nb, city = row["pin_area"], None, row["district"] or "", row["city_name"] or ""
        when, title, suspect, src = (row["first_seen"] or "")[:10], row["title"], None, "map_rounded"
    if rent is None and full:
        rent = 0
    eq = None
    if deposit or rent:
        eq = ((rent or 0) + (deposit or 0) * RAHN_MONTHLY_RATE) / 1000
    area_ok = bool(area) and 8 <= area <= 5000
    return {
        "token": row["token"], "source": "divar", "title": title or "", "neighborhood": nb, "city": city,
        "category": "commercial", "raw_category_slug": row["category_slug"], "deal_type": "rent",
        "price_total": round(eq, 2) if eq else None, "price_unit": "thousand_toman_monthly",
        "deposit": deposit or None, "monthly_rent": rent if rent is not None else None,
        "is_full_deposit": int(full), "is_negotiable": int(not deposit and not rent),
        "area_sqm": area, "rent_per_sqm": round(eq / area, 2) if (eq and area_ok) else None,
        "lat": round(float(lat), 6), "lng": round(float(lng), 6),
        "lat_is_estimated": int(bool(approx)), "has_exact_location": int(not approx),
        "on_map": int(bool(row["on_map"])), "price_source": src, "suspect_price": suspect,
        "floor": floor, "url": f"https://divar.ir/v/{row['token']}", "date_scraped": when,
        "last_seen": row["last_seen"],
    }


def export_all(cfg: LiveConfig, conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or open_db(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = iso(now_utc() - timedelta(days=cfg.stale_days))
        rows = conn.execute("SELECT * FROM listings WHERE gone=0 AND last_seen>=?", (cutoff,)).fetchall()
        recs = [r for r in (build_record(x) for x in rows) if r]
        recs.sort(key=lambda r: (r["city"], r["neighborhood"], r["date_scraped"]), reverse=False)
        out_dir = cfg.out_dir
        write_atomic(out_dir / "data.json", json.dumps(recs, ensure_ascii=False))
        n_hist = export_history(recs, out_dir / "history.json")
        n_cmp = export_comparison(recs, read_stores(cfg.stores_files), out_dir / "store_comparison.csv")
        return {"exported": len(recs), "exact_prices": sum(r["price_source"] == "detail" for r in recs),
                "history_points": n_hist, "stores_compared": n_cmp}
    finally:
        conn.row_factory = None
        if own:
            conn.close()


def _eq_psqm(r: dict) -> float | None:
    if not r.get("area_sqm") or not (8 <= r["area_sqm"] <= 5000):
        return None
    eq = (r.get("monthly_rent") or 0) + (r.get("deposit") or 0) * MAP_RAHN
    return eq / r["area_sqm"] if eq > 0 else None


def export_history(recs: list[dict], path: Path) -> int:
    """One point per (city, neighbourhood, day): median monthly-equivalent per m², thousand Toman
    (the unit real_estate_map.html expects for history rows)."""
    today = now_utc().strftime("%Y-%m-%d")
    groups: dict[tuple[str, str], list[float]] = {}
    for r in recs:
        v = _eq_psqm(r)
        if v is None or r.get("suspect_price"):
            continue
        groups.setdefault((r["city"], r["neighborhood"]), []).append(v)
        groups.setdefault((r["city"], ""), []).append(v)
    old = []
    if path.exists():
        try:
            old = [h for h in json.loads(path.read_text(encoding="utf-8") or "[]") if h.get("date") != today]
        except ValueError:
            old = []
    new = [{"city": c, "neighborhood": nb, "category": "commercial", "deal_type": "rent", "date": today,
            "median_psqm": round(median(v) / 1000, 2), "n": len(v)}
           for (c, nb), v in groups.items() if len(v) >= 3]
    rows = sorted(old + new, key=lambda h: (h["city"], h["neighborhood"], h["date"]))
    write_atomic(path, json.dumps(rows, ensure_ascii=False))
    return len(rows)


def store_equivalent(s: dict) -> float | None:
    """Port of storeEquivalent() in real_estate_map.html: cash rent (or % of sales) + deposit × RAHN."""
    monthly, dep = num(s.get("monthly_rent_toman")), num(s.get("deposit_toman")) or 0
    sales, ann = num(s.get("avg_monthly_sales_toman")), num(s.get("avg_annual_sales_toman"))
    if sales and ann and not (0.3 <= ann / (sales * 12) <= 1.5):
        ann = None
    if sales is None and ann:
        sales = ann / 12
    pct, min_r = num(s.get("sales_rent_pct")), num(s.get("minimum_rent_toman"))
    frac = (pct / 100 if pct and pct > 1 else pct) if pct is not None else None
    from_sales = sales * frac if (sales is not None and frac is not None) else None
    if from_sales is not None and monthly and abs(monthly - from_sales) / max(from_sales, 1) > 0.4:
        monthly = None
    st = str(s.get("deal_structure") or "").strip().lower()
    if st not in ("fixed", "percent_of_sales", "hybrid"):
        st = "hybrid" if (min_r is not None and pct is not None) else (
            "percent_of_sales" if (sales is not None and pct is not None and monthly is None) else "fixed")
    d = dep * MAP_RAHN
    if st == "fixed":
        return None if (monthly is None and not dep) else (monthly or 0) + d
    if st == "percent_of_sales":
        base = from_sales if from_sales is not None else monthly
        return None if base is None else base + d
    if from_sales is not None:
        base = max(from_sales, min_r or 0)
    elif min_r:
        base = min_r
    elif monthly is not None:
        base = monthly
    else:
        return None
    return base + d


def export_comparison(recs: list[dict], stores: list[dict], path: Path) -> int:
    """A quick table: each store against the listings around it (the map does the full weighted model)."""
    cols = ["brand", "store_name", "city", "store_area_sqm", "store_monthly_equiv_toman", "store_equiv_per_sqm",
            "comps_1km", "comps_3km", "median_equiv_per_sqm_3km", "median_rent_per_sqm_3km",
            "median_deposit_per_sqm_3km", "p25_equiv_per_sqm_3km", "p75_equiv_per_sqm_3km",
            "gap_vs_market_pct", "verdict"]
    rows = []
    for s in stores:
        area = num(s.get("area_sqm"))
        eq = store_equivalent(s)
        near = [(haversine_km(s["lat"], s["lng"], r["lat"], r["lng"]), r) for r in recs]
        c1 = [r for km, r in near if km <= 1]
        c3 = [r for km, r in near if km <= 3 and _eq_psqm(r) is not None and not r.get("suspect_price")]
        ps = sorted(_eq_psqm(r) for r in c3)
        med = median(ps)
        mine = eq / area if (eq and area) else None
        gap = (mine / med - 1) * 100 if (mine and med) else None
        verdict = ("insufficient data" if len(ps) < 5 or gap is None else
                   "above market" if gap > 15 else "below market" if gap < -15 else "at market")
        q = (lambda f: ps[min(int(f * len(ps)), len(ps) - 1)] if ps else None)
        rows.append({
            "brand": s.get("brand"), "store_name": s["store_name"], "city": s["city"], "store_area_sqm": area,
            "store_monthly_equiv_toman": round(eq) if eq else None,
            "store_equiv_per_sqm": round(mine) if mine else None, "comps_1km": len(c1), "comps_3km": len(ps),
            "median_equiv_per_sqm_3km": round(med) if med else None,
            "median_rent_per_sqm_3km": round(median([r["monthly_rent"] / r["area_sqm"] for r in c3
                                                     if r.get("monthly_rent")]) or 0) or None,
            "median_deposit_per_sqm_3km": round(median([r["deposit"] / r["area_sqm"] for r in c3
                                                        if r.get("deposit")]) or 0) or None,
            "p25_equiv_per_sqm_3km": round(q(0.25)) if ps else None,
            "p75_equiv_per_sqm_3km": round(q(0.75)) if ps else None,
            "gap_vs_market_pct": round(gap, 1) if gap is not None else None, "verdict": verdict})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)
    return len(rows)


# ---------------------------------------------------------------- entry points
def run_once(cfg: LiveConfig, progress: Progress | None = None, demo: bool = False) -> dict:
    """Synchronous wrapper used by the CLI and by live_map_server.py."""
    if demo:
        from live_demo import install_fake_divar
        install_fake_divar(read_stores(cfg.stores_files))
    t0 = time.time()
    col = Collector(cfg, progress)
    error = None
    try:
        stats = asyncio.run(col.run())
    except Aborted as e:
        error, stats = str(e), col.stats
    summary = {"finished_at": iso(now_utc()), "seconds": round(time.time() - t0, 1), "demo": demo,
               "error": error, "errors": col.errors[:20], **stats}
    write_atomic(cfg.out_dir / "last_run.json", json.dumps(summary, ensure_ascii=False, indent=1))
    return summary


def parse_cities(values: list[str]) -> list[tuple[str, float]]:
    out = []
    for v in values or []:
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, r = part.partition(":")
            out.append((name.strip(), float(r) if r else 15.0))
    return out


def config_from_args(a: argparse.Namespace) -> LiveConfig:
    cfg = LiveConfig()
    if a.stores:
        cfg.stores_files = [Path(p) for p in a.stores]
    cfg.radius_km = a.radius_km
    cfg.categories = [c.strip() for c in a.categories.split(",") if c.strip()]
    cfg.extra_cities = parse_cities(a.city)
    cfg.max_details = a.max_details
    cfg.include_unpinned = not a.no_unpinned
    cfg.stale_days = a.stale_days
    cfg.out_dir = Path(a.out_dir) if a.out_dir else (DEMO_DIR if getattr(a, "demo", False) else OUT_DIR)
    return cfg


def add_common_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--stores", nargs="*", help="store CSV/JSON files (default: myli_stores.csv, hyperstar_stores.csv)")
    ap.add_argument("--radius-km", type=float, default=6.0, help="collect listings this far around each store")
    ap.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES), help="Divar category slugs")
    ap.add_argument("--city", action="append", default=[], help="extra whole city, e.g. 'تهران:18' (km radius)")
    ap.add_argument("--max-details", type=int, default=1500, help="ad pages opened per run")
    ap.add_argument("--no-unpinned", action="store_true", help="skip ads that have no map pin")
    ap.add_argument("--stale-days", type=int, default=3, help="drop ads not seen for this many days")
    ap.add_argument("--out-dir", default=None, help="default live_data/ (live_data_demo/ with --demo)")
    ap.add_argument("--demo", action="store_true", help="offline demo: synthetic Divar, no network")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_common_args(sub.add_parser("run", help="collect from Divar and export"))
    add_common_args(sub.add_parser("export", help="re-export from the local database"))
    a = ap.parse_args()
    cfg = config_from_args(a)
    if a.cmd == "export":
        print(json.dumps(export_all(cfg), ensure_ascii=False, indent=1))
        return

    last = {"t": 0.0}

    def progress(phase, done, total, msg=""):
        if time.time() - last["t"] > 2 or done == total:
            last["t"] = time.time()
            log.info(f"[{phase}] {done}/{total} {msg}")

    summary = run_once(cfg, progress, demo=a.demo)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
