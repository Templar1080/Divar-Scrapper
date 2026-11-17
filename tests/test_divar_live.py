"""Offline tests for divar_live.py (fake Divar via httpx.MockTransport — no network).

    pip install -r requirements-live.txt pytest
    python -m pytest -q tests/
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import divar_live as dl  # noqa: E402
from live_demo import FakeDivar  # noqa: E402
from divar_mcp import http as dhttp  # noqa: E402

STORE = {"store_name": "تست", "city": "تهران", "lat": 35.75, "lng": 51.40, "area_sqm": "120",
         "monthly_rent_toman": "120000000", "deposit_toman": "0", "deal_structure": "fixed",
         "neighborhood_or_street": "ونک"}


def make_cfg(tmp_path, stores_rows, **kw):
    f = tmp_path / "stores.json"
    f.write_text(json.dumps(stores_rows, ensure_ascii=False), encoding="utf-8")
    cfg = dl.LiveConfig(stores_files=[f], out_dir=tmp_path / "out", **kw)
    return cfg


def install(fake):
    dhttp.set_transport(httpx.MockTransport(fake.handle))
    dhttp.JITTER = (0.0, 0.0)
    return fake


def run(cfg):
    return dl.run_once(cfg)


def test_dense_area_is_split_so_nothing_is_cut_off(tmp_path):
    fake = FakeDivar([STORE], seed=1)
    # make one 2 km tile hold ~900 pinned shop ads (the map returns only 200 per call)
    for i in range(900):
        tok = f"DENSE{i:05d}"
        fake.ads[tok] = {**next(iter(fake.ads.values())), "token": tok, "slug": "shop-rent", "pinned": True,
                         "lat": 35.7501 + (i % 30) * 0.0003, "lng": 51.4001 + (i // 30) * 0.0003}
    install(fake)
    cfg = make_cfg(tmp_path, [STORE], radius_km=3, include_unpinned=False, max_details=0)
    s = run(cfg)
    got = {r["token"] for r in json.loads((cfg.out_dir / "data.json").read_text(encoding="utf-8"))}
    assert all(f"DENSE{i:05d}" in got for i in range(900))
    assert s.get("truncated_tiles", 0) == 0


def test_exact_prices_and_incremental_runs(tmp_path):
    fake = install(FakeDivar([STORE], seed=2))
    cfg = make_cfg(tmp_path, [STORE], radius_km=4, max_details=10_000)
    s1 = run(cfg)
    assert s1["error"] is None and s1["exported"] > 50 and s1["details_pending"] == 0
    recs = json.loads((cfg.out_dir / "data.json").read_text(encoding="utf-8"))
    for r in recs:
        a = fake.ads[r["token"]]
        assert r["price_source"] == "detail"
        assert r["deposit"] == a["deposit"] and (r["monthly_rent"] or 0) == a["rent"]
        assert r["area_sqm"] == a["area"] and r["neighborhood"] == a["district"]
        assert r["lat_is_estimated"] == int(a["approx"]) or not a["pinned"]
        assert r["deal_type"] == "rent" and r["category"] == "commercial"
    # second run: nothing new to open
    s2 = run(cfg)
    assert s2["new_ads"] == 0 and s2["details_fetched"] == 0
    # an ad disappears from Divar → it leaves the map once its page 404s / it is stale
    gone = recs[0]["token"]
    del fake.ads[gone]
    run(cfg)
    still = {r["token"] for r in json.loads((cfg.out_dir / "data.json").read_text(encoding="utf-8"))}
    assert gone in still            # not seen this run, but within stale_days → kept for now
    import sqlite3
    with sqlite3.connect(cfg.db_path) as c:      # pretend that run was 4 days ago
        c.execute("UPDATE listings SET last_seen='2000-01-01T00:00:00Z' WHERE token=?", (gone,))
    dl.export_all(cfg)
    still = {r["token"] for r in json.loads((cfg.out_dir / "data.json").read_text(encoding="utf-8"))}
    assert gone not in still


def test_unpinned_ads_get_the_neighbourhood_centroid(tmp_path):
    fake = install(FakeDivar([STORE], seed=3))
    cfg = make_cfg(tmp_path, [STORE], radius_km=4, max_details=0)
    run(cfg)
    recs = json.loads((cfg.out_dir / "data.json").read_text(encoding="utf-8"))
    unp = [r for r in recs if not r["on_map"]]
    assert unp, "expected some ads without a map pin"
    assert all(r["lat_is_estimated"] == 1 for r in unp)
    # placed on the neighbourhood centroid (= the store point in the fake) until the ad page gives a location
    assert all(abs(r["lat"] - STORE["lat"]) < 1e-6 and abs(r["lng"] - STORE["lng"]) < 1e-6 for r in unp)
    assert any(not fake.ads[r["token"]]["pinned"] for r in unp)


def test_blocked_by_divar_aborts_cleanly(tmp_path):
    dhttp.set_transport(httpx.MockTransport(lambda req: httpx.Response(403, json={"message": "blocked"})))
    cfg = make_cfg(tmp_path, [STORE], radius_km=6)
    s = run(cfg)
    assert s["error"] and "reachable" in s["error"]
    assert json.loads((cfg.out_dir / "data.json").read_text(encoding="utf-8")) == []


def test_store_equivalent_matches_map_formula():
    assert dl.store_equivalent({"deal_structure": "fixed", "monthly_rent_toman": "100", "deposit_toman": "1000"}) \
        == pytest.approx(100 + 1000 * dl.MAP_RAHN)
    # hybrid: max(sales × pct, minimum) + deposit × RAHN
    assert dl.store_equivalent({"deal_structure": "hybrid", "avg_monthly_sales_toman": "1000",
                                "sales_rent_pct": "0.1", "minimum_rent_toman": "150"}) == pytest.approx(150)


def test_tile_grid_covers_the_circle():
    a = dl.Area("x", 35.7, 51.4, 6.0)
    cells = dl.root_tiles(a, 2.0)
    for lat, lng in [(35.7, 51.4), (35.7 + 5.9 / 111.32, 51.4), (35.7, 51.4 - 5.9 / (111.32 * 0.81))]:
        assert any(b[0] <= lat < b[2] and b[1] <= lng < b[3] for b in (dl.cell_bbox(c, 2.0) for c in cells))
