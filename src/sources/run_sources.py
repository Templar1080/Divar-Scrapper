"""اجرای اسکرپرهای چندمنبعی.

نمونه‌ها:
    python run_sources.py --init-config                     # ساخت config_sources.json
    python run_sources.py --source sheypoor --city tehran --pages 3
    python run_sources.py --source all --cities tehran,isfahan --pages 2
    python run_sources.py --fixture sample.html --source kilid --city tehran   # تست آفلاین
    python run_sources.py --merge                           # ادغام با data.json دیوار

خروجی: data/multisource.jsonl (یک آگهی در هر خط، اسکیمای مشترک)
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.base import (Listing, SourceConfig, load_config, save_jsonl, existing_keys)  # noqa: E402
from sources.adapters import DEFAULT_CONFIGS, crawl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data/multisource.jsonl"
CFG = ROOT / "src/sources/config_sources.json"

CITY_SLUGS = {"تهران": "tehran", "اصفهان": "isfahan", "کرج": "karaj", "شیراز": "shiraz",
              "مشهد": "mashhad", "تبریز": "tabriz", "قم": "qom", "اهواز": "ahvaz"}


def init_config():
    CFG.write_text(json.dumps([c.__dict__ for c in DEFAULT_CONFIGS], ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("→", CFG)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all")
    ap.add_argument("--city", default="تهران")
    ap.add_argument("--cities", default="")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--fixture", default="")
    ap.add_argument("--init-config", action="store_true")
    ap.add_argument("--merge", action="store_true", help="ادغام jsonl با data.json دیوار")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.init_config:
        init_config()
        return

    if a.merge:
        merge()
        return

    if not CFG.exists():
        init_config()
    cfgs = load_config(CFG)
    pick = [c for c in cfgs if a.source in ("all", c.name) and c.enabled]
    if not pick:
        print("منبعی مطابق انتخاب پیدا نشد. اول --init-config بزن.")
        return

    cities = [c.strip() for c in (a.cities or a.city).split(",") if c.strip()]
    prior = existing_keys(OUT)
    total_k = total_d = 0
    for c in pick:
        for city in cities:
            slug = CITY_SLUGS.get(city, city)
            print(f"— {c.name} · {city}")
            rows = crawl(c, slug, a.pages, Path(a.fixture) if a.fixture else None)
            print(f"  استخراج: {len(rows)}")
            if a.dry_run:
                for r in rows[:5]:
                    print("   ", r.as_dict())
                continue
            k, d = save_jsonl(rows, OUT, prior=prior)
            prior |= existing_keys(OUT)
            total_k += k; total_d += d
            print(f"  افزوده: {k} · تکراری/ردشده: {d}")
    if not a.dry_run:
        print(f"\nمجموع: {total_k} آگهی تازه → {OUT}")


def merge():
    """ادغام خروجی چندمنبعی با data.json دیوار در یک فایل واحد برای پایپ‌لاین."""
    rows = []
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    src = ROOT / "data/data.json"
    base = json.loads(src.read_text(encoding="utf-8"))
    seen = {(r.get("source"), r.get("token")) for r in rows}
    added = 0
    for r in base:
        t = ("divar", r.get("token"))
        if t in seen:
            continue
        r2 = dict(r)
        r2["source"] = "divar"
        r2["price_unit"] = "toman"
        rows.append(r2)
        added += 1
    p = ROOT / "data/data_all.json"
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    by = {}
    for r in rows:
        by[r.get("source", "?")] = by.get(r.get("source", "?"), 0) + 1
    print(f"→ {p}  |  {len(rows)} آگهی  |  {by}")


if __name__ == "__main__":
    main()
