"""هسته‌ی مشترک اسکرپرهای چندمنبعی.

هر منبع فقط باید یک چیز بدهد: فهرستی از dict با کلیدهای «خام».
بقیه (نرمال‌سازی واحد، نگاشت فیلد، حذف تکراری، نوشتن) اینجاست.
"""
from __future__ import annotations
import hashlib, json, re, time, unicodedata
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable

FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
SCHEMA_FIELDS = ["token", "source", "title", "neighborhood", "city", "category", "deal_type",
                 "price_total", "deposit", "monthly_rent", "is_full_deposit", "area_sqm",
                 "lat", "lng", "floor", "build_year", "url", "date_scraped", "price_unit"]


def norm_fa(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).translate(FA)
    return (s.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
             .replace("٬", "").replace(",", "").strip())


def num(v) -> float | None:
    """عدد از هر چیزی: '۱٬۲۰۰٬۰۰۰ تومان' → 1200000"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = norm_fa(str(v))
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def toman(v, unit_hint: str = "") -> float | None:
    """هر ورودی را به تومان تبدیل می‌کند.

    unit_hint: 'rial' | 'toman' | 'million_toman' | 'thousand_toman' | ''
    اگر خالی باشد، از خودِ متنِ مقدار («ریال»/«تومان») خوانده می‌شود؛
    پیش‌فرض بازار ایران برای آگهی = تومان.
    """
    x = num(v)
    if x is None:
        return None
    h = (unit_hint or "").lower()
    if not h and isinstance(v, str):
        if "ریال" in v or "rial" in v.lower():
            h = "rial"
        elif "میلیون" in v and "تومان" in v:
            h = "million_toman"
        elif "هزار" in v and "تومان" in v:
            h = "thousand_toman"
    if h == "rial":
        return x / 10.0
    if h == "million_toman":
        return x * 1_000_000
    if h == "thousand_toman":
        return x * 1_000
    return x


@dataclass
class Listing:
    token: str
    source: str
    title: str = ""
    neighborhood: str = ""
    city: str = ""
    category: str = "commercial"
    deal_type: str = "rent"
    price_total: float | None = None      # میلیون تومان (فقط برای فروش)
    deposit: float | None = None          # تومان
    monthly_rent: float | None = None     # تومان
    is_full_deposit: int = 0
    area_sqm: float | None = None
    lat: float | None = None
    lng: float | None = None
    floor: str | None = None
    build_year: str | None = None
    url: str = ""
    date_scraped: str = ""
    price_unit: str = "toman"

    def ok(self) -> tuple[bool, str]:
        if not self.area_sqm or not (5 <= self.area_sqm <= 3000):
            return False, "area"
        if not (self.deposit or self.monthly_rent or self.price_total):
            return False, "price"
        if self.lat is not None and not (24 <= self.lat <= 40):
            return False, "lat"
        return True, ""

    def key(self) -> str:
        """کلید یکتاسازی بین منابع: مکانِ گردشده + متراژ + قیمت."""
        geo = f"{round(self.lat or 0, 3)},{round(self.lng or 0, 3)}"
        price = round((self.deposit or 0) + (self.monthly_rent or 0) * 12 + (self.price_total or 0), -3)
        return hashlib.md5(f"{geo}|{round(self.area_sqm or 0, -1)}|{price}".encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in SCHEMA_FIELDS}


@dataclass
class SourceConfig:
    name: str
    base_url: str
    api_url: str = ""           # اگر API/XML/JSON دارد
    listing_path: str = ""      # مسیر آگهی در HTML (اگر لازم بود)
    letters: str = "all"        # کاراکترهای جست‌وجو برای صفحه‌بندی
    delay_s: float = 2.5
    enabled: bool = True
    notes: str = ""
    headers: dict = field(default_factory=dict)


def load_config(path: Path) -> list[SourceConfig]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [SourceConfig(**r) for r in raw]


def save_jsonl(rows: Iterable[Listing], path: Path, dedupe: dict | None = None,
               prior: set | None = None) -> tuple[int, int]:
    """نوشتن با حذف تکراری. خروجی: (نوشته‌شده، تکراری/رد‌شده)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set(prior or ())
    seen_keys = dedupe if dedupe is not None else {}
    kept = dup = 0
    with path.open("a", encoding="utf-8") as f:
        for it in rows:
            k = it.key()
            if k in seen or (it.source, it.token) in seen_keys:
                dup += 1
                continue
            good, _ = it.ok()
            if not good:
                dup += 1
                continue
            seen.add(k)
            seen_keys[(it.source, it.token)] = 1
            f.write(json.dumps(it.as_dict(), ensure_ascii=False) + "\n")
            kept += 1
    return kept, dup


def existing_keys(path: Path) -> set:
    ks = set()
    if not path.exists():
        return ks
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ks.add((d.get("source"), d.get("token")))
    return ks
