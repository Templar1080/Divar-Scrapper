"""آداپتورهای استخراج برای منابع ایرانی املاک.

وضعیت صادقانه:
    • ساختار هر سه سایت (شیپور، کلید، دیوار) با یک لایه‌ی «حدسِ ساختاریافته» خوانده
      می‌شود: اول JSON امبدشده در صفحه (__NEXT_DATA__ / __NUXT__ / JSON-LD)، بعد
      API عمومی، بعد regex روی HTML.
    • سایت‌های ایرانی از این محیط اجرا (sandbox) قابل دسترسی نیستند؛ بنابراین
      اتصال باید از سیستم خودت تست شود. برای همین، هر آداپتور هم `--fixture` دارد
      تا با یک فایل HTML/JSON ذخیره‌شده تست شود.
    • کلیدهای فارسی/انگلیسی متنوع پوشش داده شده‌اند تا وابستگی به نام‌گذاری یک نسخه‌ی
      خاص کم شود.
"""
from __future__ import annotations
import json, re, time
from pathlib import Path
from typing import Iterable
from .base import Listing, SourceConfig, norm_fa, num, toman

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"}

# ---------------------------------------------------------------- استخراج JSON
JSON_BLOCKS = [
    (re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S), "next"),
    (re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", re.S), "nuxt"),
    (re.compile(r"<script type=\"application/ld\+json\">(.*?)</script>", re.S), "ld"),
]


def _walk(node, out, depth=0):
    """همه‌ی dictهایی که «شبیه آگهی» هستند را جمع می‌کند."""
    if depth > 12:
        return
    if isinstance(node, dict):
        ks = {str(k).lower() for k in node.keys()}
        has_price = any(k in ks for k in ("price", "amount", "deposit", "rahn", "rent",
                                          "price_value", "cost", "قیمت", "ودیعه", "اجاره"))
        has_area = any(k in ks for k in ("area", "size", "meterage", "building_size",
                                         "metrage", "متراژ", "مساحت"))
        if has_price and has_area:
            out.append(node)
        for v in node.values():
            _walk(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, depth + 1)


def _pick(d: dict, *names, default=None):
    for n in names:
        for k in d:
            if str(k).lower() == n:
                return d[k]
    return default


def _deep_get(d: dict, *path, default=None):
    cur = d
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur


# ---------------------------------------------------------------- نرمال‌سازی
def to_listing(raw: dict, source: str, city: str = "", base_url: str = "") -> Listing | None:
    title = norm_fa(str(_pick(raw, "title", "name", "headline", "subject", "عنوان") or ""))
    area = num(_pick(raw, "area", "size", "meterage", "building_size", "metrage",
                     "مساحت", "متراژ"))
    price_raw = _pick(raw, "price", "amount", "total_price", "cost", "قیمت", "قیمت کل")
    dep_raw = _pick(raw, "deposit", "rahn", "prepayment", "ودیعه", "رهن", "پیش پرداخت")
    rent_raw = _pick(raw, "rent", "monthly_rent", "اجاره", "اجاره ماهانه")
    # بعضی سایت‌ها قیمت را dict می‌دهند: {"value": 1200000000, "unit": "toman"}
    def flat(v):
        if isinstance(v, dict):
            u = str(_pick(v, "unit", "currency", "type") or "")
            return _pick(v, "value", "amount", "price"), u
        return v, ""
    p, pu = flat(price_raw)
    d_, du = flat(dep_raw)
    r_, ru = flat(rent_raw)

    blob = json.dumps(raw, ensure_ascii=False)
    rial_hint = ("ریال" in blob) and ("تومان" not in blob)
    unit = "rial" if (rial_hint or "rial" in (pu + du + ru).lower()) else ""

    deposit = toman(d_, unit or du)
    monthly = toman(r_, unit or ru)
    price = toman(p, unit or pu)
    # price_total در این پروژه «میلیون تومان» است
    price_m = round(price / 1_000_000, 3) if price else None

    lat = num(_deep_get(raw, "location", "latitude") or _deep_get(raw, "location", "lat")
              or _pick(raw, "lat", "latitude"))
    lng = num(_deep_get(raw, "location", "longitude") or _deep_get(raw, "location", "long")
              or _pick(raw, "lng", "lon", "longitude"))
    nb = norm_fa(str(_pick(raw, "neighborhood", "district", "region", "محله", "منطقه") or ""))
    token = str(_pick(raw, "token", "id", "uid", "code", "identifier") or
                _deep_get(raw, "link", "token") or "")[:40]
    url = str(_pick(raw, "url", "link", "web_view_link", "permalink") or "")
    if url.startswith("/"):
        url = base_url.rstrip("/") + url
    deal = "rent" if (deposit or monthly or "اجاره" in title or "رهن" in title) else "sale"

    li = Listing(token=token or f"{source}-{abs(hash(json.dumps(raw, sort_keys=True))) % 10**10}",
                 source=source, title=title, neighborhood=nb, city=city,
                 deal_type=deal, price_total=price_m, deposit=deposit, monthly_rent=monthly,
                 is_full_deposit=int(bool(deposit) and not monthly),
                 area_sqm=area, lat=lat, lng=lng,
                 floor=str(_pick(raw, "floor", "طبقه") or "") or None,
                 build_year=str(_pick(raw, "build_year", "year", "سال ساخت") or "") or None,
                 url=url, date_scraped=time.strftime("%Y-%m-%d"),
                 price_unit="toman")
    good, _ = li.ok()
    return li


# ---------------------------------------------------------------- هر منبع
DEFAULT_CONFIGS = [
    SourceConfig(name="divar", base_url="https://divar.ir",
                 api_url="https://api.divar.ir/v8/postlist/w/search",
                 letters="all", delay_s=3.0, enabled=True,
                 notes="اسکرپر کامل و تست‌شده در data/divar_scraper.py؛ این آداپتور فقط "
                       "خروجی آن را به اسکیمای مشترک تبدیل می‌کند."),
    SourceConfig(name="sheypoor", base_url="https://www.sheypoor.com",
                 api_url="https://www.sheypoor.com/api/v10.0.0/listings",
                 listing_path="/v/{token}", delay_s=3.0, enabled=True,
                 notes="آگهی‌های املاک: /املاک/تجاری-صنعتی ; بهتر است با کوکی مرورگر اجرا شود."),
    SourceConfig(name="kilid", base_url="https://kilid.com",
                 api_url="https://api.kilid.com/v1/search",
                 listing_path="/buy-residential/{token}", delay_s=3.0, enabled=True,
                 notes="کلید روی «فروش/اجاره» متمرکز است؛ برای مقایسه‌ی ودیعه/اجاره مفید است."),
]


def fetch_html(url: str, cfg: SourceConfig, timeout: int = 25) -> str:
    import urllib.request
    hdr = dict(UA); hdr.update(cfg.headers or {})
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def parse_html(html: str, source: str, city: str = "", base_url: str = "") -> list[Listing]:
    found: list[dict] = []
    for rx, kind in JSON_BLOCKS:
        for m in rx.finditer(html):
            txt = m.group(1).strip()
            try:
                js = json.loads(txt)
            except Exception:
                continue
            _walk(js, found)
    # fallback: تگ‌های data-* یا microdata
    if not found:
        for m in re.finditer(r'itemprop="(price|name|url)"[^>]*content="([^"]*)"', html):
            found.append({m.group(1): m.group(2)})
    out = []
    for raw in found[:4000]:
        li = to_listing(raw, source, city, base_url)
        if li:
            out.append(li)
    return out


def crawl(cfg: SourceConfig, city: str, pages: int = 3, fixture: Path | None = None) -> list[Listing]:
    """خزش یک منبع. اگر fixture داده شود، از فایل محلی می‌خواند (تست آفلاین)."""
    if fixture:
        txt = Path(fixture).read_text(encoding="utf-8", errors="ignore")
        return parse_html(txt, cfg.name, city, cfg.base_url)
    out: list[Listing] = []
    for p in range(1, pages + 1):
        try:
            if cfg.api_url:
                url = cfg.api_url
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}city={city}&page={p}"
            else:
                url = f"{cfg.base_url}/s/{city}?page={p}"
            html = fetch_html(url, cfg)
            got = parse_html(html, cfg.name, city, cfg.base_url)
            out.extend(got)
            if not got:
                break
        except Exception as e:  # noqa: BLE001
            print(f"  ! {cfg.name} page {p}: {type(e).__name__}: {e}")
            break
        time.sleep(cfg.delay_s)
    return out
