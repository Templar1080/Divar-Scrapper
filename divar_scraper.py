"""
divar_scraper.py  (v6)
بازنویسی کامل با اصلاحات:
• رفع باگ برچسب پاس کل‌شهری: حالا "__whole__{city}" تا آگهی‌های بدون محله‌ی رسمی
  برچسب جعلی نگیرند و کشف محله‌ها خراب نشود.
• کشف محله از BREADCRUMB (اسم نمایشی) + منبع دوم JOIN روی district_enum.
• عمق تطبیقی: همه‌ی محله‌های کشف‌شده «پاس پایه»، محله‌های پرتراکم و
  محله‌های فروشگاه‌ها «پاس کامل».
• توقف زودهنگام: دو صفحه‌ی پیاپیِ کاملاً تکراری ⇒ پایان پاس.
• scrape-metro چندشهره: --city تهران,شیراز,اهواز,مشهد
استفاده:
python divar_scraper.py scrape-metro --city تهران,شیراز,اهواز,مشهد --stores-file myli_stores.csv
python divar_scraper.py scrape-city --city شیراز --baseline-pages 3
"""
import argparse, json, logging, random, re, sqlite3, statistics, time, os, signal, threading
from collections import Counter
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ---------------------------------------------------------------- تنظیمات
DB_PATH = "divar_listings.db"
EXPORT_PATH = "data.json"
HISTORY_EXPORT_PATH = "history.json"
API_BASE = "https://api.divar.ir/v8"
SEARCH_URL = f"{API_BASE}/postlist/w/search"
FIELDS_SEARCH_URL = f"{API_BASE}/fields-search"
POST_DETAIL_URL = f"{API_BASE}/posts-v2/web"

CITY_IDS = {
    "تهران": "1", "کرج": "2", "مشهد": "3", "اصفهان": "4",
    "شیراز": "6", "اهواز": "7", "ارومیه": "10", "لواسان": "1771", "پرند": "1766",
    "کرمان": "13", "بندرعباس": "18", "قم": "8", "سیرجان": "818", "بابلسر": "710",
    "کلارآباد": "1695", "شاهین‌شهر": "1744", "اسلامشهر": "29",
}
PROVINCE_CITIES = {
    "تهران": ["اسلامشهر", "پرند", "شهریار", "ملارد", "قدس", "رباط‌کریم",
              "پردیس", "بومهن", "پاکدشت", "ورامین", "دماوند", "فیروزکوه", "لواسان"],
    "کرج": ["فردیس", "ماهدشت", "نظرآباد", "هشتگرد", "کمال‌شهر", "اشتهارد"],
    "اصفهان": ["شاهین‌شهر", "خمینی‌شهر", "نجف‌آباد", "فلاورجان", "مبارکه",
               "زرین‌شهر", "باغ بهادران", "فولادشهر"],
    "مشهد": ["طرقبه", "شاندیز", "چناران", "گلبهار"],
    "شیراز": ["صدرا", "زرقان"],
    "بابلسر": ["بابل", "فریدونکنار", "کلارآباد"],
    "قم": [], "اهواز": ["بندر امام خمینی", "ماهشهر"],
    "کرمان": ["رفسنجان", "سیرجان"], "ارومیه": [], "بندرعباس": ["میناب", "قشم"],
}
HEADERS = {
    "Content-Type": "application/json", "Accept": "application/json",
    "Origin": "https://divar.ir", "Referer": "https://divar.ir/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
MIN_DELAY, MAX_DELAY = 2.5, 5.0
MAX_RETRIES = 4
RAHN_MONTHLY_RATE = 0.03
SALE_MIN_MTOMAN = 200
SALE_MAX_MTOMAN = 2_000_000
DAILY_HINTS = ("روزانه", "شبانه", "مبله", "اجاره روز", "هر شب")
SQM_PRICE_HINTS = ("قیمت هر متر", "متری", "هر متر مربع", "قیمت متری")

WHOLE_CITY_PAGES_DEFAULT = 30
DISTRICT_PAGES_DEFAULT = 8
BASELINE_PAGES_DEFAULT = 3
MAX_DISTRICT_PASSES_DEFAULT = 60

CITY_CENTERS = {
    "تهران": (35.70, 51.40), "کرج": (35.8400, 50.9391), "مشهد": (36.28, 59.60),
    "اصفهان": (32.6546, 51.6680), "شیراز": (29.61, 52.53), "اهواز": (31.3183, 48.6706),
    "ارومیه": (37.5527, 45.0761), "لواسان": (35.8172, 51.6357),
    "کرمان": (30.2839, 57.0834), "بندرعباس": (27.1865, 56.2808), "قم": (34.6416, 50.8746),
    "سیرجان": (29.4519, 55.6814), "بابلسر": (36.7025, 52.6564), "کلارآباد": (36.65, 51.30),
    "شاهین‌شهر": (32.8598, 51.5537), "پرند": (35.4792, 50.9422),
    "اسلامشهر": (35.5567, 51.2222), "شهریار": (35.6597, 51.0592),
    "ملارد": (35.6644, 50.9767), "رباط‌کریم": (35.4850, 51.0781),
    "پردیس": (35.7550, 51.8250), "پاکدشت": (35.4822, 51.6797),
    "ورامین": (35.3269, 51.6453), "قدس": (35.7211, 51.1108),
    "دماوند": (35.7178, 52.7736), "فردیس": (35.7367, 50.9853),
    "ماهدشت": (35.8100, 50.8300), "نظرآباد": (35.9497, 50.6167),
    "هشتگرد": (35.9700, 50.6800), "کمال‌شهر": (35.8650, 50.8850),
    "اشتهارد": (35.7256, 50.3486), "خمینی‌شهر": (32.6997, 51.5581),
    "نجف‌آباد": (32.6339, 51.3667), "فلاورجان": (32.6389, 51.6081),
    "مبارکه": (32.3392, 51.5039), "زرین‌شهر": (32.3283, 51.4636),
    "باغ بهادران": (32.2833, 51.2333), "طرقبه": (36.2725, 59.4650),
    "شاندیز": (36.3253, 59.4267), "چناران": (36.6417, 59.1200),
    "گلبهار": (36.4489, 59.2178), "صدرا": (29.8194, 52.5228),
    "بابل": (36.5511, 52.6789), "فریدونکنار": (36.6903, 52.5236),
    "بومهن": (35.7515, 51.8514), "فیروزکوه": (35.7561, 52.7736),
    "زرقان": (29.7914, 52.7097), "فولادشهر": (32.5872, 51.4972),
    "رفسنجان": (30.4067, 56.1606), "میناب": (27.0886, 57.0872),
    "قشم": (26.8000, 55.9333), "ماهشهر": (30.4583, 49.1931),
    "بندر امام خمینی": (30.2833, 49.0667),
}
NEIGHBORHOOD_CENTROIDS = {
    "تجریش": (35.8044, 51.4327), "نیاوران": (35.8129, 51.4661),
    "فرمانیه": (35.8090, 51.4620), "زعفرانیه": (35.8073, 51.4283),
    "الهیه": (35.7996, 51.4188), "ولنجک": (35.8060, 51.3960),
    "قیطریه": (35.7930, 51.4400), "دروس": (35.7830, 51.4440),
    "پاسداران": (35.7889, 51.4453), "قلهک": (35.7734, 51.4442),
    "ونک": (35.7594, 51.4108), "جردن": (35.7776, 51.4157),
    "میرداماد": (35.7599, 51.4265), "ظفر": (35.7700, 51.4300),
    "گاندی": (35.7500, 51.4130), "آرژانتین": (35.7350, 51.4180),
    "یوسف‌آباد": (35.7290, 51.4020), "امیرآباد": (35.7350, 51.3900),
    "سعادت‌آباد": (35.7809, 51.3702), "شهرک غرب": (35.7620, 51.3690),
    "پونک": (35.7480, 51.3300), "جنت‌آباد": (35.7570, 51.3100),
    "شهران": (35.7700, 51.3250), "صادقیه": (35.7160, 51.3400),
    "مرزداران": (35.7350, 51.3450), "ستارخان": (35.7150, 51.3500),
    "گیشا": (35.7350, 51.3760), "نارمک": (35.7350, 51.5050),
    "تهرانپارس": (35.7370, 51.5350), "رسالت": (35.7440, 51.4900),
    "مجیدیه": (35.7350, 51.4750), "سیدخندان": (35.7480, 51.4530),
    "انقلاب": (35.7010, 51.3910), "کریمخان": (35.7180, 51.4180),
    "فاطمی": (35.7250, 51.4050), "سهروردی": (35.7280, 51.4350),
    "شوش": (35.6650, 51.4100), "مولوی": (35.6720, 51.4150),
    "نازی‌آباد": (35.6470, 51.3950), "یافت‌آباد": (35.6440, 51.3350),
    "خانی‌آباد": (35.6350, 51.4050), "شهرری": (35.5890, 51.4270),
    "دولت‌آباد": (35.6280, 51.4450), "افسریه": (35.6420, 51.4800),
    "پیروزی": (35.6950, 51.4550), "هفده شهریور": (35.6850, 51.4600),
    "چیتگر جنوبی": (35.7350, 51.1950), "آزادی": (35.6990, 51.3450),
    "لویزان": (35.8150, 51.5100),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler("scraper.log", encoding="utf-8"),
                              logging.StreamHandler()])
log = logging.getLogger("divar_scraper")
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# ================= pause/resume + سرعت =================
PAUSE_FILE = Path("divar_pause.flag")
DETAIL_WORKERS = 2
METRO_WORKERS = 2
GLOBAL_MIN_INTERVAL = float(os.environ.get("DIVAR_GLOBAL_MIN_INTERVAL", "0.80"))
SKIP_ALREADY_SCRAPED_TODAY = True
_DB_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_last_request_at = 0.0
_throttle_until = 0.0
_last_sigint = 0.0
_tls_http = threading.local()

def is_paused(): return PAUSE_FILE.exists()
def pause_scraper(): PAUSE_FILE.touch()
def resume_scraper():
    try: PAUSE_FILE.unlink()
    except FileNotFoundError: pass
def wait_if_paused():
    while is_paused(): time.sleep(1.0)
def interruptible_sleep(seconds):
    if seconds <= 0: wait_if_paused(); return
    end = time.time() + seconds
    while True:
        wait_if_paused()
        remaining = end - time.time()
        if remaining <= 0: break
        time.sleep(min(0.5, max(0.05, remaining)))
def _http_session():
    if not hasattr(_tls_http, "s"): _tls_http.s = requests.Session()
    return _tls_http.s
def _global_rate_wait():
    global _last_request_at
    while True:
        wait_if_paused()
        with _RATE_LOCK:
            now = time.time()
            delay = (_throttle_until - now) if _throttle_until > now \
                else _last_request_at + GLOBAL_MIN_INTERVAL - now
            if delay <= 0:
                _last_request_at = now; return
        time.sleep(delay + random.uniform(0.0, 0.15))
def _report_http_status(code):
    global _throttle_until
    if code in (429, 403, 503):
        with _RATE_LOCK:
            _throttle_until = max(_throttle_until, time.time() + 12 + random.uniform(0, 8))
def install_pause_handlers():
    def _handler(signum, frame):
        global _last_sigint
        now = time.time()
        if now - _last_sigint < 2.0: raise KeyboardInterrupt
        _last_sigint = now
        if is_paused(): resume_scraper(); print("[Resume] Continuing...")
        else: pause_scraper(); print(f"[Pause] Paused. Ctrl+C once=resume, or delete {PAUSE_FILE}.")
    try: signal.signal(signal.SIGINT, _handler)
    except Exception: pass
    if is_paused(): print(f"[Pause] Pause file exists: {PAUSE_FILE}.")

# ---------------------------------------------------------------- مدل داده
@dataclass
class Listing:
    token: str; title: str; neighborhood: str; city: str; category: str
    raw_category_slug: str; deal_type: str
    price_total: Optional[float]; deposit: Optional[float]; monthly_rent: Optional[float]
    is_full_deposit: int; is_negotiable: int
    area_sqm: Optional[float]; rooms: Optional[int]
    lat: Optional[float]; lng: Optional[float]; lat_is_estimated: int
    has_exact_location: int = 0
    floor: Optional[str] = None; build_year: Optional[str] = None
    district_enum: Optional[str] = None; description: Optional[str] = None
    url: str = ""
    date_scraped: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

# ---------------------------------------------------------------- parsing
def fa_to_en_digits(s): return (s or "").translate(_DIGIT_MAP)
def normalize_number(s):
    if not s: return ""
    return (fa_to_en_digits(s).replace("٬", "").replace(",", "").replace("،", "")
            .replace("\u200c", " "))
def _norm_key(s):
    t = (s or "").replace("\u200c", "").replace("\u00a0", "").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", "", t).strip()
def _norm_name(s):
    return (s or "").replace("\u200c", "").replace(" ", "").strip().replace("ي", "ی").replace("ك", "ک")
def row_number(v):
    if v is None: return None
    m = re.search(r"\d[\d,]*", normalize_number(str(v)))
    if not m: return None
    try: return float(m.group(0).replace(",", ""))
    except ValueError: return None
def _texts_of(data):
    return [data.get("middle_description_text") or "", data.get("top_description_text") or "",
            data.get("bottom_description_text") or ""]
def parse_area(title, texts):
    for src in [title] + list(texts):
        m = re.search(r"(\d{2,4})\s*متر", normalize_number(src))
        if m:
            v = float(m.group(1))
            if 5 <= v <= 5000: return v
    return None
def parse_rooms(title, texts):
    for src in [title] + list(texts):
        t = normalize_number(src)
        if "بدون اتاق" in t or "بدون خواب" in t: return 0
        m = re.search(r"(\d)\s*(?:خواب|اتاق)", t)
        if m: return int(m.group(1))
    return None
def parse_price_sale(texts):
    joined = " ".join(texts)
    if any(h in joined for h in DAILY_HINTS): return None, False
    negotiable = ("توافقی" in joined) or ("تماس" in joined)
    for t in texts:
        if any(h in t for h in SQM_PRICE_HINTS): continue
        m = re.search(r"(\d{8,})", t.replace(",", "").replace("٬", ""))
        if m:
            val = int(m.group(1)) / 1_000_000
            if SALE_MIN_MTOMAN <= val <= SALE_MAX_MTOMAN: return val, negotiable
    return None, negotiable
def parse_price_rent(texts):
    joined = " | ".join(normalize_number(t) for t in texts if t)
    if not joined: return None, None, False, False
    negotiable = "توافقی" in joined
    full_deposit = "رهن کامل" in joined
    deposit = monthly = None
    m = re.search(r"(?:رهن|ودیعه)\s*:?\s*(\d{6,})", joined)
    if m: deposit = float(m.group(1))
    m = re.search(r"اجاره\s*:?\s*(\d{5,})", joined)
    if m: monthly = float(m.group(1))
    elif re.search(r"اجاره\s*:?\s*(?:مجانی|رایگان|0)\b", joined): monthly = 0.0
    if full_deposit and monthly is None: monthly = 0.0
    return deposit, monthly, full_deposit, negotiable
def rahn_to_monthly_equivalent(deposit, monthly_rent, monthly_rate=RAHN_MONTHLY_RATE):
    if deposit is None and monthly_rent is None: return None
    return ((monthly_rent or 0.0) + (deposit or 0.0) * monthly_rate) / 1000
def build_post_detail_url(token): return f"https://divar.ir/v/{token}"

# ---------------------------------------------------------------- HTTP
def request_with_retry(session, url, payload=None, method="POST"):
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _global_rate_wait()
            resp = session.post(url, json=payload, headers=HEADERS, timeout=20) \
                if method == "POST" else session.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 404: return None
            if resp.status_code in (429, 403, 503):
                _report_http_status(resp.status_code); resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"[!] تلاش {attempt}/{MAX_RETRIES} روی {url} شکست خورد: {e}")
            if attempt == MAX_RETRIES: return None
            interruptible_sleep(delay + random.uniform(0, 1)); delay *= 2
    return None

def build_payload(category_slug, district_ids, city_id="1", pagination_data=None):
    if pagination_data is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        pagination_data = {"@type": "type.googleapis.com/post_list.PaginationData",
                           "last_post_date": now, "page": 1, "layer_page": 1,
                           "first_page_viewed_at": now}
    form_data = {"category": {"str": {"value": category_slug}}}
    if district_ids:
        form_data["districts"] = {"repeated_string": {"value": list(district_ids)}}
    return {"city_ids": [city_id], "source_view": "FILTER",
            "pagination_data": pagination_data, "disable_recommendation": False,
            "map_state": {"camera_info": {"bbox": {}}},
            "search_data": {"form_data": {"data": form_data},
                            "server_payload": {"@type": "type.googleapis.com/widgets.SearchData.ServerPayload",
                                               "additional_form_data": {"data": {"sort": {"str": {"value": "sort_date"}}}}}}}

# ---------------------------------------------------------------- fields-search
def _looks_related(q, m):
    if not q or not m: return False
    shorter, longer = sorted([q, m], key=len)
    if shorter not in longer: return False
    return len(shorter) / len(longer) >= 0.6
def resolve_city_id(session, city_name):
    city_name = (city_name or "").strip()
    if not city_name: return None
    if city_name in CITY_IDS: return CITY_IDS[city_name]
    res = request_with_retry(session, FIELDS_SEARCH_URL,
        {"field": "cities", "search_key": "cities", "q": city_name, "query": city_name, "source": "filter"}, method="POST")
    results = (res or {}).get("results") or []
    if not results: return None
    exact = next((r for r in results if _norm_name(r.get("enumName")) == _norm_name(city_name)), None)
    if exact: pick = exact
    else:
        best = results[0]
        if not _looks_related(_norm_name(city_name), _norm_name(best.get("enumName"))):
            log.warning(f"[!] شهر «{city_name}» فازی مچ شد ولی مرتبط نیست؛ رد شد."); return None
        pick = best
    city_id = str(pick.get("enum")) if pick.get("enum") is not None else None
    if city_id: log.info(f"[=] شهر «{city_name}» → city_id {city_id} ({pick.get('enumName')}) [fields-search]")
    return city_id
def normalize_address_text(s):
    if not s: return s
    s = s.replace("\u00a0", " ").replace("\u200c", " ")
    s = re.sub(r"[;؛]", " ", s); return re.sub(r"\s+", " ", s).strip()
def resolve_district_ids(session, names, city_id="1"):
    out = {}
    for name in names:
        res = request_with_retry(session, FIELDS_SEARCH_URL,
            {"field": "districts", "search_key": "districts", "q": name, "query": name,
             "source": "filter", "cities": [city_id], "city_id": int(city_id)}, method="POST")
        results = (res or {}).get("results") or []
        if not results:
            log.warning(f"[!] محله‌ی «{name}» پیدا نشد؛ رد شد."); continue
        exact = next((r for r in results if _norm_name(r.get("enumName")) == _norm_name(name)), None)
        if exact: pick = exact
        else:
            best = results[0]
            if _looks_related(_norm_name(name), _norm_name(best.get("enumName"))): pick = best
            else:
                log.warning(f"[!] «{name}» فازی مچ شد ولی مرتبط نیست؛ رد شد."); continue
        out[name] = str(pick.get("enum"))
        log.info(f"[=] {name} → enum {out[name]} ({pick.get('enumName')})")
        time.sleep(random.uniform(1.0, 2.0))
    return out

# ---------------------------------------------------------------- صفحه جزئیات (v6: district_name)
def _iter_widgets(widgets):
    stack = list(widgets or [])
    while stack:
        w = stack.pop(); yield w
        wl = ((w.get("data") or {}).get("modal_page") or {}).get("widget_list")
        if wl: stack.extend(wl)
def extract_detail(detail):
    out = {"lat": None, "lng": None, "exact": False, "desc": "", "rows": {},
           "district_enum": None, "district_name": None, "category_slug": None}
    if not detail: return out
    all_widgets = []
    for sec in (detail.get("sections") or []):
        sw = sec.get("widgets") or []; all_widgets.extend(sw)
        if sec.get("section_name") == "DESCRIPTION":
            for w in sw:
                if w.get("widget_type") == "DESCRIPTION_ROW":
                    out["desc"] = (w.get("data") or {}).get("text") or ""
    for w in _iter_widgets(all_widgets):
        wt = w.get("widget_type"); d = w.get("data") or {}
        if wt == "MAP_ROW":
            loc = d.get("location") or {}
            pt = ((loc.get("exact_data") or {}).get("point")) or {}
            if pt.get("latitude") is not None and pt.get("longitude") is not None:
                out["lat"], out["lng"] = pt["latitude"], pt["longitude"]; out["exact"] = True
            else:
                fz = ((loc.get("fuzzy_data") or {}).get("point")) or {}
                if fz.get("latitude") is not None and fz.get("longitude") is not None:
                    out["lat"], out["lng"] = fz["latitude"], fz["longitude"]
        elif wt == "UNEXPANDABLE_ROW":
            t_, v_ = (d.get("title") or "").strip(), (d.get("value") or "").strip()
            if t_ and t_ not in out["rows"]: out["rows"][t_] = v_
        elif wt == "GROUP_INFO_ROW":
            for it in (d.get("items") or []):
                t_, v_ = (it.get("title") or "").strip(), (it.get("value") or "").strip()
                if t_ and t_ not in out["rows"]: out["rows"][t_] = v_
        elif wt == "BREADCRUMB":
            for pi in (d.get("parent_items") or []):
                pi_title = (pi.get("title") or "").strip()   # v6: اسم نمایشی محله
                sd = (((pi.get("action") or {}).get("payload") or {}).get("search_data") or {})
                cand_datas = [((sd.get("form_data") or {}).get("data")) or {},
                              (((sd.get("server_payload") or {}).get("additional_form_data") or {}).get("data")) or {}]
                for cd in cand_datas:
                    val = (((cd.get("districts") or {}).get("repeated_string") or {}).get("value")) or []
                    if val and not out["district_enum"]:
                        out["district_enum"] = str(val[0])
                        if pi_title and not out["district_name"]:
                            out["district_name"] = pi_title
                    cv = ((cd.get("category") or {}).get("str") or {}).get("value")
                    if cv and not out["category_slug"]: out["category_slug"] = cv
    return out

ROW_FIELD_MAP = {
    "ودیعه": "deposit", "وديعهماهانه": "deposit", "اجارهماهانه": "monthly_rent",
    "قيمتكل": "sale_total", "قيمتفروش": "sale_total",
    "متراژ": "area", "متراژ(مترمربع)": "area", "متراژزمين": "area_land", "متراژزمین": "area_land",
    "اتاق": "rooms", "اتاقخواب": "rooms", "طبقه": "floor",
    "ساخت": "build_year", "سالساخت": "build_year", "سال": "build_year",
}
def structured_fields(rows):
    got, area_cands = {}, []
    for title, value in rows.items():
        key = ROW_FIELD_MAP.get(_norm_key(title))
        if not key: continue
        if key == "rooms":
            n = row_number(value); got[key] = int(n) if n is not None else None
        elif key == "floor": got[key] = fa_to_en_digits(str(value)).strip() or None
        elif key == "build_year": got[key] = fa_to_en_digits(str(value)).strip() or None
        elif key in ("area", "area_land"):
            n = row_number(value)
            if n is not None: area_cands.append(n)
        else: got[key] = row_number(value)
    if area_cands:
        valid = [a for a in area_cands if 8 <= a <= 5000]
        got["area"] = valid[0] if valid else max(area_cands)
    return got

# ---------------------------------------------------------------- طبقه‌بندی
COMMERCIAL_KEYWORDS = ("مغازه", "دفتر", "اداری", "تجاری", "انبار", "سالن", "کارگاه", "غرفه", "سرقفلی")
RENT_KEYWORDS = ("رهن", "اجاره", "ودیعه")
COMMERCIAL_SLUG_HINTS = ("shop", "office", "industry", "agriculture", "commercial", "workshop")
def classify_category(title, blob, category_slug):
    slug = (category_slug or "").lower()
    if any(h in slug for h in COMMERCIAL_SLUG_HINTS): return "commercial"
    if slug and any(h in slug for h in ("apartment", "villa", "house", "suite", "plot", "land", "old-property")): return "residential"
    if any(k in blob for k in COMMERCIAL_KEYWORDS): return "commercial"
    return "residential"

# ---------------------------------------------------------------- مختصات fallback
def jitter_coords(lat, lng, seed_text, radius_deg=0.0027):
    import hashlib
    if lat is None or lng is None: return lat, lng
    h = int(hashlib.md5((seed_text or "x").encode("utf-8")).hexdigest()[:8], 16)
    dlat = ((h % 971) - 485) / 971.0 * radius_deg
    dlng = ((h // 971 % 887) - 443) / 887.0 * radius_deg
    return round(lat + dlat, 6), round(lng + dlng, 6)
def refine_neighborhood_from_names(text_norm, names_by_len, requested):
    for name, norm in names_by_len:
        if norm and norm in text_norm: return name
    return requested

# ---------------------------------------------------------------- دیتابیس
NEW_COLUMNS = [("has_exact_location", "INTEGER DEFAULT 0"), ("floor", "TEXT"), ("build_year", "TEXT"),
               ("district_enum", "TEXT"), ("description", "TEXT")]
def _create_listings_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS listings (
        token TEXT, title TEXT, neighborhood TEXT, city TEXT, category TEXT,
        raw_category_slug TEXT, deal_type TEXT, price_total REAL, deposit REAL,
        monthly_rent REAL, is_full_deposit INTEGER, is_negotiable INTEGER,
        area_sqm REAL, rooms INTEGER, lat REAL, lng REAL, lat_is_estimated INTEGER,
        has_exact_location INTEGER DEFAULT 0, floor TEXT, build_year TEXT,
        district_enum TEXT, description TEXT, url TEXT, date_scraped TEXT,
        is_outlier INTEGER DEFAULT 0, PRIMARY KEY (token, date_scraped))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS districts (
        city_id TEXT, enum TEXT, name TEXT, PRIMARY KEY (city_id, enum))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS district_centroids (
        city TEXT, neighborhood TEXT, lat REAL, lng REAL, samples INTEGER,
        PRIMARY KEY (city, neighborhood))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_group ON listings(city, neighborhood, category, deal_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token ON listings(token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_est ON listings(lat_is_estimated)")
    conn.commit()
def _add_missing_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    for name, decl in NEW_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
            log.info(f"[i] ستون {name} اضافه شد.")
    conn.commit()
def init_db(path=DB_PATH):
    with _DB_LOCK:
        conn = sqlite3.connect(path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA busy_timeout=300000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _create_listings_table(conn); _add_missing_columns(conn)
        return conn
def upsert_district(conn, city_id, enum, name):
    if not city_id or not enum or not name: return
    try:
        conn.execute("""INSERT INTO districts (city_id, enum, name) VALUES (?,?,?)
            ON CONFLICT(city_id, enum) DO UPDATE SET name=excluded.name""",
            (str(city_id), str(enum), name.strip()))
    except sqlite3.Error as e: log.debug(f"district upsert failed: {e}")
def district_names(conn, city_id):
    rows = conn.execute("SELECT DISTINCT name FROM districts WHERE city_id=?", (str(city_id),)).fetchall()
    seen, out = set(), []
    for (n,) in rows:
        nn = _norm_name(n)
        if n and nn and nn not in seen: seen.add(nn); out.append((n, nn))
    out.sort(key=lambda t: -len(t[1])); return out
def update_district_centroids(conn, min_samples=3):
    rows = conn.execute("""SELECT city, neighborhood, lat, lng FROM listings
        WHERE has_exact_location=1 AND lat IS NOT NULL AND lng IS NOT NULL
          AND neighborhood IS NOT NULL AND neighborhood != ''""").fetchall()
    groups = {}
    for city, nb, lat, lng in rows: groups.setdefault((city, nb), []).append((lat, lng))
    updated = 0
    for (city, nb), pts in groups.items():
        if len(pts) < min_samples: continue
        mlat = statistics.median(p[0] for p in pts); mlng = statistics.median(p[1] for p in pts)
        conn.execute("""INSERT INTO district_centroids (city, neighborhood, lat, lng, samples)
            VALUES (?,?,?,?,?) ON CONFLICT(city, neighborhood) DO UPDATE SET
            lat=excluded.lat, lng=excluded.lng, samples=excluded.samples""",
            (city, nb, round(mlat, 6), round(mlng, 6), len(pts)))
        updated += 1
    conn.commit()
    if updated: log.info(f"[i] مرکز {updated} محله از پین‌های دقیق بازسازی شد (district_centroids).")
def get_neighborhood_centroid(conn, city, neighborhood):
    r = conn.execute("SELECT lat, lng FROM district_centroids WHERE city=? AND neighborhood=?",
                     (city, neighborhood)).fetchone()
    if r: return (r[0], r[1])
    return NEIGHBORHOOD_CENTROIDS.get(neighborhood)

# ---------------------------------------------------------------- outlier/save/export
def compute_outlier(l):
    if l.area_sqm is not None and not (8 <= l.area_sqm <= 5000): return 1
    if l.deal_type == "sale":
        if l.price_total is not None:
            if not (SALE_MIN_MTOMAN <= l.price_total <= SALE_MAX_MTOMAN): return 1
            if l.area_sqm and 8 <= l.area_sqm <= 5000:
                ppsm = l.price_total / l.area_sqm
                if not (0.3 <= ppsm <= 800): return 1
    else:
        if (l.deposit is not None and l.deposit < 10_000_000 and not l.is_full_deposit): return 1
    return 0
LISTING_COLS = ["token", "title", "neighborhood", "city", "category", "raw_category_slug",
    "deal_type", "price_total", "deposit", "monthly_rent", "is_full_deposit", "is_negotiable",
    "area_sqm", "rooms", "lat", "lng", "lat_is_estimated", "has_exact_location",
    "floor", "build_year", "district_enum", "description", "url", "date_scraped"]
def save_listing(conn, listing):
    try:
        d = asdict(listing)
        ph = ",".join(f":{c}" for c in LISTING_COLS); collist = ",".join(LISTING_COLS)
        conn.execute(f"INSERT OR REPLACE INTO listings ({collist}) VALUES ({ph})",
                     {**d, "is_outlier": compute_outlier(listing)})
        return True
    except sqlite3.Error as e:
        log.error(f"[x] خطای دیتابیس روی {listing.token}: {e}"); return False
def flag_outliers(conn):
    cur = conn.cursor(); cur.execute("UPDATE listings SET is_outlier=0")
    groups = cur.execute("""SELECT DISTINCT city, neighborhood, category, deal_type FROM listings
        WHERE price_total IS NOT NULL AND area_sqm IS NOT NULL AND area_sqm > 0""").fetchall()
    flagged = 0
    for city, nb, cat, dt in groups:
        rows = cur.execute("""SELECT rowid, price_total, area_sqm FROM listings
            WHERE city=? AND neighborhood=? AND category=? AND deal_type=?
              AND price_total IS NOT NULL AND area_sqm > 0""", (city, nb, cat, dt)).fetchall()
        if len(rows) < 4: continue
        psqm = sorted(r[1] / r[2] for r in rows)
        q1, q3 = psqm[len(psqm)//4], psqm[(3*len(psqm))//4]; iqr = q3 - q1
        low, high = q1 - 1.5*iqr, q3 + 1.5*iqr
        for rowid, price, area in rows:
            v = price / area
            if v < low or v > high:
                cur.execute("UPDATE listings SET is_outlier=1 WHERE rowid=?", (rowid,)); flagged += 1
    conn.commit(); log.info(f"[i] {flagged} رکورد به‌عنوان outlier پرچم‌گذاری شد.")
def export_json(conn, path=EXPORT_PATH, include_outliers=False):
    where = "" if include_outliers else "WHERE is_outlier=0"
    cols = ["token", "title", "neighborhood", "city", "category", "deal_type", "price_total",
            "deposit", "monthly_rent", "is_full_deposit", "area_sqm", "rooms", "lat", "lng",
            "lat_is_estimated", "has_exact_location", "floor", "build_year", "url",
            "date_scraped", "is_negotiable"]
    rows = conn.execute(f"SELECT {','.join(cols)} FROM listings {where} "
                        f"ORDER BY neighborhood, deal_type, date_scraped DESC").fetchall()
    out, seen = [], set()
    for r in rows:
        d = dict(zip(cols, r))
        if d["token"] in seen: continue
        seen.add(d["token"])
        a = d["area_sqm"]; valid = bool(a) and 8 <= a <= 5000
        d["price_per_sqm"] = None; d["rent_per_sqm"] = None
        if valid and d["price_total"]:
            if d["deal_type"] == "sale":
                d["price_unit"] = "million_toman"; d["price_per_sqm"] = round(d["price_total"]/a, 3)
            else:
                d["price_unit"] = "thousand_toman_monthly"; d["rent_per_sqm"] = round(d["price_total"]/a, 2)
        else:
            d["price_unit"] = "million_toman" if d["deal_type"] == "sale" else "thousand_toman_monthly"
        out.append(d)
    Path(path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log.info(f"[✓] {len(out)} آگهی در {path} خروجی گرفته شد.")
def export_history_json(conn, path=HISTORY_EXPORT_PATH):
    cur = conn.cursor()
    rows = cur.execute("""SELECT city, neighborhood, category, deal_type, date_scraped,
        price_total, area_sqm, is_full_deposit FROM listings
        WHERE is_outlier=0 AND price_total IS NOT NULL AND area_sqm > 0""").fetchall()
    groups = {}
    for city, nb, cat, deal, date, price, area, is_full in rows:
        if deal == "rent" and is_full: continue
        groups.setdefault((city, nb, cat, deal, date), []).append(price / area)
    out = []
    for (city, nb, cat, deal, date), values in groups.items():
        values.sort(); n = len(values)
        median = values[n//2] if n % 2 else (values[n//2-1] + values[n//2]) / 2
        out.append({"city": city, "neighborhood": nb, "category": cat, "deal_type": deal,
                    "date": date, "median_psqm": round(median, 2), "n": n})
    out.sort(key=lambda r: (r["city"], r["neighborhood"], r["category"], r["deal_type"], r["date"]))
    Path(path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log.info(f"[✓] {len(out)} نقطه‌ی روند (گروه×تاریخ) در {path} خروجی گرفته شد.")

# ---------------------------------------------------------------- ساخت Listing
def build_listing(row_data, detail_ex, conn, ctx):
    token = row_data.get("token")
    if not token: return None
    web_info = row_data.get("web_info") or {}
    title = row_data.get("title") or ""
    official_nb = web_info.get("district_persian") or ""
    city = web_info.get("city_persian") or ctx["city_name"]
    texts = _texts_of(row_data)
    list_blob = " | ".join(normalize_number(t) for t in texts if t)
    sf = structured_fields(detail_ex.get("rows") or {})
    desc = detail_ex.get("desc") or ""
    full_blob = f"{title} {list_blob} {normalize_number(desc)}"
    cat_slug = detail_ex.get("category_slug")
    if sf.get("deposit") is not None or sf.get("monthly_rent") is not None \
            or "رهن" in full_blob or "ودیعه" in full_blob: deal_type = "rent"
    elif sf.get("sale_total") is not None: deal_type = "sale"
    else: deal_type = "rent" if any(k in full_blob for k in RENT_KEYWORDS) else "sale"
    category = classify_category(title, full_blob, cat_slug)
    deposit = monthly_rent = None; is_full_deposit = is_negotiable = False; price_total = None
    if deal_type == "rent":
        deposit, monthly_rent = sf.get("deposit"), sf.get("monthly_rent")
        if deposit is None and monthly_rent is None:
            deposit, monthly_rent, is_full_deposit, is_negotiable = parse_price_rent(texts)
        is_negotiable = ("توافقی" in full_blob) and deposit is None and monthly_rent is None
        if monthly_rent == 0 or ("رهن کامل" in full_blob and monthly_rent is None):
            is_full_deposit = True; monthly_rent = monthly_rent if monthly_rent is not None else 0.0
        price_total = rahn_to_monthly_equivalent(deposit, monthly_rent)
    else:
        st = sf.get("sale_total")
        if st is not None: price_total = st / 1_000_000; is_negotiable = "توافقی" in full_blob
        else: price_total, is_negotiable = parse_price_sale(texts)
    area = sf.get("area")
    if area is None: area = parse_area(title, texts + ([desc] if desc else []))
    rooms = sf.get("rooms")
    if rooms is None: rooms = parse_rooms(title, texts + ([desc] if desc else []))
    neighborhood = official_nb or ctx["fallback_neighborhood"]
    if desc and conn is not None:
        known = district_names(conn, ctx["city_id"])
        if known:
            neighborhood = refine_neighborhood_from_names(_norm_name(desc + " " + title), known, neighborhood)
    if detail_ex.get("exact"):
        lat, lng = detail_ex["lat"], detail_ex["lng"]; estimated = 0
    else:
        base = get_neighborhood_centroid(conn, city, neighborhood) if conn is not None else None
        if base is None:
            fb = ctx.get("fallback_coords"); base = fb if fb else CITY_CENTERS.get(ctx["city_name"])
        lat, lng = jitter_coords(base[0] if base else None, base[1] if base else None, token)
        estimated = 1
        if detail_ex.get("lat") is not None and not detail_ex.get("exact"):
            lat, lng = detail_ex["lat"], detail_ex["lng"]
    return Listing(token=token, title=title, neighborhood=neighborhood, city=city,
        category=category, raw_category_slug=cat_slug or ctx.get("category_slug", ""),
        deal_type=deal_type, price_total=price_total, deposit=deposit, monthly_rent=monthly_rent,
        is_full_deposit=int(is_full_deposit), is_negotiable=int(is_negotiable),
        area_sqm=area, rooms=rooms, lat=lat, lng=lng, lat_is_estimated=estimated,
        has_exact_location=int(bool(detail_ex.get("exact"))), floor=sf.get("floor"),
        build_year=sf.get("build_year"), district_enum=detail_ex.get("district_enum"),
        url=build_post_detail_url(token), description=desc)

# ---------------------------------------------------------------- حلقه‌های پاس (v6)
def run_pass(session, conn, city_id, city_name, targets, district_enum,
             pass_label, max_pages, stats):
    total_saved = 0
    for target in targets:
        slug = target["category_slug"]; pagination_data = None; dup_streak = 0
        for page in range(1, max_pages + 1):
            wait_if_paused()
            payload = build_payload(slug, [district_enum] if district_enum else [], city_id, pagination_data)
            result = request_with_retry(session, SEARCH_URL, payload, method="POST")
            if not result:
                log.warning(f"[!] {pass_label}/{slug} صفحه {page}: پاسخی نگرفتم."); break
            widgets = [x for x in (result.get("list_widgets") or []) if x.get("widget_type") == "POST_ROW"]
            if not widgets:
                log.info(f"[i] {pass_label}/{slug} صفحه {page}: آگهی‌ای نبود."); break
            items = []
            for x in widgets:
                data = x.get("data") or {}
                payload_a = (data.get("action") or {}).get("payload") or {}
                token = payload_a.get("token") or data.get("token")
                if token: items.append((token, data, payload_a))
            tokens = [t for t, _, _ in items]; already = set()
            if tokens and SKIP_ALREADY_SCRAPED_TODAY:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                qmarks = ",".join("?" * len(tokens))
                with _DB_LOCK:
                    rows = conn.execute(f"SELECT token FROM listings WHERE date_scraped=? AND token IN ({qmarks})",
                                        [today] + tokens).fetchall()
                already = {r[0] for r in rows}
            to_fetch, skipped = [], 0
            for token, data, payload_a in items:
                if token in already:
                    skipped += 1
                    web_info = payload_a.get("web_info") or {}
                    official_nb = web_info.get("district_persian") or ""
                    nb_for_pairing = official_nb or ("" if pass_label.startswith("__whole__") else pass_label)
                    stats[(city_name, nb_for_pairing or pass_label)] += 1
                else: to_fetch.append((token, data, payload_a))
            # v6: توقف زودهنگام روی دو صفحه‌ی پیاپیِ کاملاً تکراری
            if items and skipped == len(items): dup_streak += 1
            else: dup_streak = 0
            if dup_streak >= 2:
                log.info(f"[i] {pass_label}/{slug}: دو صفحه‌ی پیاپی تکراری؛ توقف زودهنگام."); break
            detail_by_token = {}
            if to_fetch:
                def _fetch_one(tok):
                    wait_if_paused()
                    return tok, request_with_retry(_http_session(), f"{POST_DETAIL_URL}/{tok}", method="GET")
                with ThreadPoolExecutor(max_workers=max(1, DETAIL_WORKERS)) as pool:
                    futs = [pool.submit(_fetch_one, tok) for tok, _, _ in to_fetch]
                    for fut in as_completed(futs):
                        try:
                            tok, detail = fut.result(); detail_by_token[tok] = detail
                        except Exception as e: log.warning(f"[!] خطا در دریافت جزئیات: {e}")
            saved = 0; prepared = []
            for token, data, payload_a in to_fetch:
                wait_if_paused()
                ex = extract_detail(detail_by_token.get(token))
                web_info = payload_a.get("web_info") or {}
                # v6: fallback به اسم محله از BREADCRUMB
                official_nb = web_info.get("district_persian") or ex.get("district_name") or ""
                row_ctx = {"city_id": city_id, "city_name": city_name,
                           "fallback_neighborhood": official_nb or ("" if pass_label.startswith("__whole__") else pass_label),
                           "fallback_coords": CITY_CENTERS.get(city_name), "category_slug": slug}
                listing = build_listing({**data, "token": token}, ex, conn, row_ctx)
                prepared.append((listing, ex, official_nb))
            with _DB_LOCK:
                for listing, ex, official_nb in prepared:
                    if listing and save_listing(conn, listing): saved += 1
                    nb_for_pairing = official_nb or ("" if pass_label.startswith("__whole__") else pass_label)
                    if nb_for_pairing:
                        if ex.get("district_enum"):
                            upsert_district(conn, city_id, ex["district_enum"], nb_for_pairing)
                        elif district_enum:
                            upsert_district(conn, city_id, district_enum, nb_for_pairing)
                    stats[(city_name, nb_for_pairing or pass_label)] += 1
                conn.commit()
            total_saved += saved
            skip_note = f"، {skipped} تکراری" if skipped else ""
            log.info(f"[+] {pass_label}/{slug} صفحه {page}: {len(widgets)} آگهی، {saved} ذخیره{skip_note}")
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next_page"):
                log.info(f"[i] {pass_label}/{slug}: صفحه‌ی آخر."); break
            pagination_data = pagination.get("data")
            if not pagination_data:
                log.warning(f"[!] {pass_label}/{slug}: pagination.data نبود؛ توقف."); break
            interruptible_sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        interruptible_sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return total_saved

# ---------------------------------------------------------------- کشف + عمق (v6)
def discover_and_deep_scrape(session, conn, city_id, city_name, targets,
                             whole_pages, district_pages, baseline_pages,
                             max_district_passes, only_districts=None,
                             priority_neighborhoods=None):
    stats = Counter()
    # v6: پاک‌سازی اسم‌های جعلی wholeX این شهر
    conn.execute("DELETE FROM districts WHERE city_id=? AND (name LIKE 'whole%' OR name LIKE '__whole__%')",
                 (str(city_id),)); conn.commit()
    whole_label = f"__whole__{city_name}"   # v6: پیشوند درست
    total = run_pass(session, conn, city_id, city_name, targets, None, whole_label, whole_pages, stats)
    # منبع ۱: نام‌های رسمی/بریدکرامبی از پاس کل‌شهری
    seen = {nb: cnt for (c, nb), cnt in stats.items()
            if c == city_name and nb and not nb.startswith("__whole__")}
    # منبع ۲ (v6): شمارش district_enum ذخیره‌شده روی آگهی‌ها
    try:
        rows = conn.execute("""SELECT d.name, COUNT(*) AS cnt FROM listings l
            JOIN districts d ON d.enum = l.district_enum AND d.city_id = ?
            WHERE l.city = ? AND l.district_enum IS NOT NULL GROUP BY d.name""",
            (str(city_id), city_name)).fetchall()
        for name, cnt in rows: seen[name] = max(seen.get(name, 0), cnt)
    except sqlite3.Error as e: log.warning(f"[!] کشف district_enum ناموفق: {e}")
    if not seen:
        log.warning(f"[!] هیچ محله‌ای برای «{city_name}» کشف نشد."); return total
    learned = dict(conn.execute("SELECT name, enum FROM districts WHERE city_id=?", (str(city_id),)).fetchall())
    if only_districts:
        wanted = [n for n in only_districts if n in seen or n in learned]
    else:
        wanted = [n for n, _ in sorted(seen.items(), key=lambda kv: -kv[1])]
    priority = list(priority_neighborhoods or [])
    for n in priority:
        if n not in wanted: wanted.append(n)
    full_set = set(p for p in priority if p in wanted)
    for name in wanted:
        if name in full_set: continue
        if len(full_set) < max_district_passes: full_set.add(name)
    picked = []
    for name in wanted:
        pages = district_pages if name in full_set else baseline_pages
        enum = learned.get(name)
        if not enum:
            resolved = resolve_district_ids(session, [name], city_id); enum = resolved.get(name)
            if enum:
                with _DB_LOCK: upsert_district(conn, city_id, enum, name); conn.commit()
        if not enum:
            log.warning(f"[!] برای «{name}» enum پیدا نشد؛ رد شد."); continue
        picked.append((name, enum, pages))
    n_full = sum(1 for _, _, p in picked if p == district_pages); n_base = len(picked) - n_full
    log.info(f"[i] پاس عمق: {n_full} محله کامل + {n_base} محله پایه از {len(seen)} محله‌ی کشف‌شده ...")
    for i, (name, enum, pages) in enumerate(picked, 1):
        wait_if_paused()
        log.info(f"[i] ({i}/{len(picked)}) عمق‌گیری «{name}» ({pages} صفحه) ...")
        total += run_pass(session, conn, city_id, city_name, targets, enum, name, pages, stats)
        interruptible_sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return total

# ---------------------------------------------------------------- ورودی‌های سطح بالا
def finalize(conn):
    with _DB_LOCK: update_district_centroids(conn); flag_outliers(conn)
def store_priority_neighborhoods(session, conn, city_id, city_name, stores_file):
    if not stores_file: return []
    try: stores = read_stores_csv(stores_file)
    except Exception as e:
        log.warning(f"[!] خواندن فایل فروشگاه‌ها ناموفق: {e}"); return []
    names = []
    for s in stores:
        if (s.get("city") or "").strip() != city_name: continue
        for cand in _address_search_candidates(s):
            if cand not in names: names.append(cand)
    if names: log.info(f"[i] اولویت فروشگاه‌های «{city_name}»: {len(names)} محله/ناحیه.")
    return names
def deep_scrape_city(session, city_name, targets, whole_pages, district_pages,
                     max_district_passes, only_districts=None, stores_file=None,
                     baseline_pages=BASELINE_PAGES_DEFAULT):
    city_id = resolve_city_id(session, city_name)
    if not city_id:
        log.error(f"[x] شهر «{city_name}» resolve نشد."); return False
    conn = init_db(DB_PATH)
    log.info(f"[i] اسکرپ عمیق «{city_name}» (city_id={city_id}) شروع شد ...")
    prio = store_priority_neighborhoods(session, conn, city_id, city_name, stores_file)
    total = discover_and_deep_scrape(session, conn, city_id, city_name, targets,
        whole_pages, district_pages, baseline_pages, max_district_passes, only_districts, prio)
    finalize(conn)
    log.info(f"[✓] «{city_name}»: مجموع {total} آگهی ذخیره شد.")
    conn.close(); return True
def _scrape_city_worker(city_name, targets, whole_pages, district_pages,
                        max_district_passes, stores_file, baseline_pages):
    try:
        wait_if_paused()
        return deep_scrape_city(requests.Session(), city_name, targets, whole_pages,
            district_pages, max_district_passes, stores_file=stores_file, baseline_pages=baseline_pages)
    except Exception as e:
        log.error(f"[x] کارگر «{city_name}» شکست خورد: {e}"); return False
def scrape_metro(city_arg, targets, whole_pages, district_pages, max_district_passes,
                 metro_workers=None, stores_file=None, baseline_pages=BASELINE_PAGES_DEFAULT):
    cities = [c.strip() for c in str(city_arg).split(",") if c.strip()]
    towns = []
    for c in cities: towns += [c] + PROVINCE_CITIES.get(c, [])
    workers = max(1, int(metro_workers or METRO_WORKERS))
    log.info(f"[i] اسکرپ کلان‌شهری: {len(cities)} کلان‌شهر ({'، '.join(cities)}) — {len(towns)} شهر با {workers} کارگر")
    init_db(DB_PATH).close()
    ok = failed = 0
    if workers == 1:
        for town in towns:
            wait_if_paused()
            if deep_scrape_city(requests.Session(), town, targets, whole_pages, district_pages,
                    max_district_passes, stores_file=stores_file, baseline_pages=baseline_pages): ok += 1
            else: failed += 1
            interruptible_sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_scrape_city_worker, town, targets, whole_pages, district_pages,
                    max_district_passes, stores_file, baseline_pages): town for town in towns}
            for fut in as_completed(futs):
                town = futs[fut]
                try:
                    if fut.result(): ok += 1
                    else: failed += 1
                except Exception as e:
                    failed += 1; log.error(f"[x] «{town}»: {e}")
    log.info(f"[✓] کلان‌شهری تمام شد: {ok} موفق، {failed} ناموفق.")

# ---------------------------------------------------------------- فروشگاه‌ها
MAX_DISTRICTS_PER_STORE = 3
def _parse_numeric_field(v):
    if v is None: return None
    v = str(v).strip()
    if v in ("", "-", "--", "؟", "نامشخص", "توافقی"): return None
    v = v.replace(",", "")
    try: return float(v)
    except ValueError: return None
def _parse_percent_field(v):
    if v is None: return None
    v = str(v).strip()
    if v in ("", "-", "--"): return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", v)]
    return sum(nums) / len(nums) if nums else None
def _parse_coordinates(s):
    if not s: return None, None
    parts = [p.strip() for p in re.split(r"[,،]", str(s)) if p.strip()]
    if len(parts) < 2: return None, None
    try: return float(parts[0]), float(parts[1])
    except ValueError: return None, None
def read_stores_csv(path):
    import csv, io
    with open(path, encoding="utf-8-sig") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    rows = list(csv.DictReader(io.StringIO("".join(lines))))
    out = []
    for r in rows:
        lat, lng = _parse_coordinates(r.get("coordinates"))
        if lat is None or lng is None:
            try: lat, lng = float(r["lat"]), float(r["lng"])
            except (KeyError, ValueError, TypeError):
                log.warning(f"[!] فروشگاه «{r.get('store_name','?')}» مختصات ندارد؛ رد شد."); continue
        r["lat"], r["lng"] = lat, lng
        r["monthly_rent_toman"] = _parse_numeric_field(r.get("monthly_rent_toman"))
        r["deposit_toman"] = _parse_numeric_field(r.get("deposit_toman"))
        r["annual_increase_pct"] = _parse_percent_field(r.get("annual_increase_pct"))
        r["area_sqm"] = _parse_numeric_field(r.get("area_sqm"))
        r["avg_monthly_sales_toman"] = _parse_numeric_field(r.get("avg_monthly_sales_toman"))
        r["avg_annual_sales_toman"] = _parse_numeric_field(r.get("avg_annual_sales_toman"))
        if r["avg_monthly_sales_toman"] is None and r["avg_annual_sales_toman"] is not None:
            r["avg_monthly_sales_toman"] = r["avg_annual_sales_toman"] / 12
        r["sales_rent_pct"] = _parse_numeric_field(r.get("sales_rent_pct"))
        r["minimum_rent_toman"] = _parse_numeric_field(r.get("minimum_rent_toman"))
        structure = (r.get("deal_structure") or "").strip().lower()
        if structure not in ("fixed", "percent_of_sales", "hybrid"):
            if r["minimum_rent_toman"] is not None and r["sales_rent_pct"] is not None: structure = "hybrid"
            elif (r["avg_monthly_sales_toman"] is not None and r["sales_rent_pct"] is not None
                  and r["monthly_rent_toman"] is None): structure = "percent_of_sales"
            else: structure = "fixed"
        r["deal_structure"] = structure
        r["province"] = (r.get("province") or "").strip(); r["county"] = (r.get("county") or "").strip()
        r["address"] = normalize_address_text(r.get("neighborhood_or_street") or r.get("address") or "")
        out.append(r)
    return out
def _address_search_candidates(store):
    address = normalize_address_text((store.get("address") or "").strip())
    candidates = [address] if address else []
    candidates += [p.strip() for p in re.split(r"[،,-–]", address) if p.strip() and len(p.strip()) > 1]
    seen, uniq = set(), []
    for c in candidates:
        if c not in seen: seen.add(c); uniq.append(c)
    return uniq
def scrape_near_stores(default_city_id, default_city_name, targets, stores_csv,
                       whole_pages=15, district_pages=8):
    stores = read_stores_csv(stores_csv)
    if not stores:
        log.error("[x] فروشگاهی پیدا نشد."); return
    session = requests.Session()
    by_city = {}
    for s in stores: by_city.setdefault((s.get("city") or default_city_name).strip(), []).append(s)
    log.info(f"[i] {len(stores)} فروشگاه در {len(by_city)} شهر.")
    summary = {"resolved": [], "unresolved_city": [], "unresolved_address": []}
    conn = init_db(DB_PATH)
    for city_name, city_stores in by_city.items():
        city_id = resolve_city_id(session, city_name)
        if not city_id:
            summary["unresolved_city"] += [s.get("store_name", "?") for s in city_stores]; continue
        district_map = {}
        for s in city_stores:
            name = s.get("store_name", "?"); resolved_here = []
            for candidate in _address_search_candidates(s):
                if len(resolved_here) >= MAX_DISTRICTS_PER_STORE: break
                hits = resolve_district_ids(session, [candidate], city_id)
                if candidate not in hits or candidate in district_map: continue
                district_map[candidate] = hits[candidate]; resolved_here.append(candidate)
                log.info(f"[=] «{name}» ({city_name}) → «{candidate}»")
            if resolved_here: summary["resolved"].append(name)
            else: summary["unresolved_address"].append(f"{name} ({city_name})")
        stats = Counter()
        for key, enum in district_map.items():
            log.info(f"[i] اسکرپ «{key}» (اطراف فروشگاه) ...")
            run_pass(session, conn, city_id, city_name, targets, enum, key, district_pages, stats)
        unresolved_in_city = [s for s in city_stores
                              if f"{s.get('store_name','?')} ({city_name})" in summary["unresolved_address"]]
        if unresolved_in_city or not district_map:
            log.info(f"[i] پاس کلی‌شهری «{city_name}» ...")
            run_pass(session, conn, city_id, city_name, targets, None,
                     f"__whole__{city_name}", whole_pages, stats)
    finalize(conn); conn.close()
    log.info("========== خلاصه ==========")
    log.info(f"✓ resolve شد: {len(summary['resolved'])}")
    if summary["unresolved_city"]: log.info(f"✗ شهر ناشناخته: {', '.join(summary['unresolved_city'])}")
    if summary["unresolved_address"]: log.info(f"⚠ محله resolve نشد: {', '.join(summary['unresolved_address'])}")

# ---------------------------------------------------------------- enrich
_tls = threading.local()
def _worker_session():
    if not hasattr(_tls, "s"): _tls.s = requests.Session()
    return _tls.s
def _enrich_fetch(token, state):
    last_reason = None
    for attempt in range(3):
        wait_if_paused()
        with state["lock"]: wait = state["pause_until"] - time.time()
        if wait > 0: interruptible_sleep(wait)
        try:
            resp = _worker_session().get(f"{POST_DETAIL_URL}/{token}", headers=HEADERS, timeout=25)
            if resp.status_code in (429, 403, 503):
                with state["lock"]:
                    state["pause_until"] = time.time() + 10 + random.uniform(0, 6)
                    state["throttled"] = state.get("throttled", 0) + 1
                last_reason = f"http{resp.status_code}"; continue
            if resp.status_code == 404: return token, None, "404"
            resp.raise_for_status()
            interruptible_sleep(random.uniform(0, 0.1))
            return token, resp.json(), None
        except Exception as e:
            last_reason = type(e).__name__; interruptible_sleep(1 + attempt * 2)
    return token, None, last_reason
def enrich(limit=None, only_estimated=True, workers=3, before_date=None):
    conn = init_db(DB_PATH)
    where = "WHERE lat_is_estimated=1 OR has_exact_location=0" if only_estimated else ""
    params = []
    if before_date:
        where = (where + " AND" if where else "WHERE") + " date_scraped < ?"; params.append(before_date)
    sql = f"SELECT token, neighborhood, city FROM listings {where} ORDER BY date_scraped DESC"
    if limit: sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    log.info(f"[i] enrich برای {len(rows)} رکورد با {workers} worker ...")
    state = {"lock": threading.Lock(), "pause_until": 0.0, "fail": 0, "throttled": 0, "reasons": {}, "t0": time.time()}
    known_by_city = {}; updated = exact_gained = missing = 0
    def process(token, old_nb, old_city, detail, fail_reason=None):
        nonlocal updated, exact_gained, missing
        ex = extract_detail(detail)
        if not detail:
            missing += 1
            if fail_reason:
                with state["lock"]: state["reasons"][fail_reason] = state["reasons"].get(fail_reason, 0) + 1
        sf = structured_fields(ex.get("rows") or {}); desc = ex.get("desc") or ""
        sets, vals = [], []
        def add(col, v): sets.append(f"{col}=?"); vals.append(v)
        if ex.get("exact") and ex.get("lat") is not None:
            add("lat", ex["lat"]); add("lng", ex["lng"]); add("lat_is_estimated", 0); add("has_exact_location", 1)
            exact_gained += 1
        elif ex.get("lat") is not None: add("lat", ex["lat"]); add("lng", ex["lng"])
        if sf.get("deposit") is not None: add("deposit", sf["deposit"])
        if sf.get("monthly_rent") is not None: add("monthly_rent", sf["monthly_rent"])
        if sf.get("sale_total") is not None: add("price_total", sf["sale_total"] / 1_000_000)
        if sf.get("area") is not None: add("area_sqm", sf["area"])
        if sf.get("rooms") is not None: add("rooms", sf["rooms"])
        if sf.get("floor"): add("floor", sf["floor"])
        if sf.get("build_year"): add("build_year", sf["build_year"])
        if ex.get("district_enum"): add("district_enum", ex["district_enum"])
        if desc: add("description", desc)
        if desc and old_city:
            known = known_by_city.get(old_city)
            if known is None: known = known_by_city[old_city] = district_names(conn, old_city)
            if known:
                better = refine_neighborhood_from_names(_norm_name(desc), known, old_nb or "")
                if better and better != old_nb: add("neighborhood", better)
        if sets:
            vals.append(token); conn.execute(f"UPDATE listings SET {', '.join(sets)} WHERE token=?", vals)
            updated += 1
        if ex.get("district_enum") and old_nb and old_city:
            cid = resolve_city_id_cached(conn, None, old_city)
            if cid: upsert_district(conn, cid, ex["district_enum"], old_nb)
    done = 0; meta = {tok: (nb, ct) for tok, nb, ct in rows}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_enrich_fetch, tok, state) for tok, _, _ in rows]
        for fut in as_completed(futs):
            token, detail, fail_reason = fut.result()
            old_nb, old_city = meta.get(token, ("", ""))
            process(token, old_nb, old_city, detail, fail_reason)
            done += 1
            if done % 100 == 0:
                conn.commit(); log.info(f"    ... {done}/{len(rows)} ({exact_gained} پین، {missing} ناموفق)")
    conn.commit(); finalize(conn); conn.close()
    log.info(f"[✓] enrich تمام شد: {updated} به‌روز، {exact_gained} پین، {missing} ناموفق.")
_city_id_cache = {}
def resolve_city_id_cached(conn, session, city_name):
    if city_name not in _city_id_cache:
        _city_id_cache[city_name] = resolve_city_id(session or requests.Session(), city_name)
    return _city_id_cache[city_name]

# ---------------------------------------------------------------- repair-coords
def repair_coordinates(db_path=DB_PATH):
    conn = init_db(db_path); cur = conn.cursor()
    rows = cur.execute("SELECT rowid, token, neighborhood, city FROM listings WHERE has_exact_location=0").fetchall()
    fixed = 0
    for rowid, token, neighborhood, city in rows:
        base = get_neighborhood_centroid(conn, city or "", neighborhood or "")
        if base is None: base = CITY_CENTERS.get(city or "")
        if not base or not token: continue
        lat, lng = jitter_coords(base[0], base[1], token)
        cur.execute("UPDATE listings SET lat=?, lng=?, lat_is_estimated=1 WHERE rowid=?", (lat, lng, rowid))
        fixed += 1
    conn.commit(); conn.close()
    log.info(f"[✓] مختصات {fixed} رکورد تخمینی بازسازی شد.")

# ---------------------------------------------------------------- CLI
CITY_NAME_DEFAULT = "تهران"
TARGETS = [{"category_slug": "real-estate"}]
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Divar scraper v6 (uniform coverage)")
    ap.add_argument("action", choices=["scrape", "export", "both", "enrich", "scrape-stores",
                                       "scrape-city", "scrape-metro", "repair-coords", "pause", "resume"])
    ap.add_argument("--include-outliers", action="store_true")
    ap.add_argument("--max-pages", type=int, default=WHOLE_CITY_PAGES_DEFAULT)
    ap.add_argument("--district-pages", type=int, default=DISTRICT_PAGES_DEFAULT)
    ap.add_argument("--baseline-pages", type=int, default=BASELINE_PAGES_DEFAULT,
                    help="صفحات پاس پایه برای همه‌ی محله‌های کشف‌شده (پیش‌فرض ۳)")
    ap.add_argument("--max-district-passes", type=int, default=MAX_DISTRICT_PASSES_DEFAULT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--all-listings", action="store_true")
    ap.add_argument("--before-date", type=str, default=None)
    ap.add_argument("--stores-file", type=str, default=None,
                    help="فایل فروشگاه‌ها؛ محله‌هایشان همیشه پاس کامل می‌گیرند")
    ap.add_argument("--city", type=str, default=None,
                    help="برای scrape-metro می‌توان چند شهر با کاما داد: تهران,شیراز,اهواز,مشهد")
    ap.add_argument("--neighborhoods", type=str, default=None)
    ap.add_argument("--metro-workers", type=int, default=METRO_WORKERS)
    ap.add_argument("--detail-workers", type=int, default=DETAIL_WORKERS)
    ap.add_argument("--request-interval", type=float, default=GLOBAL_MIN_INTERVAL)
    args = ap.parse_args()
    if args.action == "pause":
        pause_scraper(); print("Paused."); raise SystemExit(0)
    if args.action == "resume":
        resume_scraper(); print("Resumed."); raise SystemExit(0)
    install_pause_handlers()
    DETAIL_WORKERS = max(1, args.detail_workers); METRO_WORKERS = max(1, args.metro_workers)
    GLOBAL_MIN_INTERVAL = max(0.1, args.request_interval)
    if args.action == "repair-coords": repair_coordinates(DB_PATH)
    if args.action == "enrich":
        enrich(limit=args.limit, only_estimated=not args.all_listings,
               workers=args.workers, before_date=args.before_date)
    if args.action == "scrape-stores":
        if not args.stores_file: log.error("[x] --stores-file لازم است.")
        else: scrape_near_stores("1", CITY_NAME_DEFAULT, TARGETS, args.stores_file,
                                 whole_pages=args.max_pages, district_pages=args.district_pages)
    if args.action == "scrape-city":
        if not args.city: log.error("[x] --city لازم است.")
        else:
            only = [n.strip() for n in args.neighborhoods.split(",")] if args.neighborhoods else None
            deep_scrape_city(requests.Session(), args.city, TARGETS, args.max_pages,
                             args.district_pages, args.max_district_passes, only_districts=only,
                             stores_file=args.stores_file, baseline_pages=args.baseline_pages)
    if args.action == "scrape-metro":
        if not args.city: log.error("[x] --city لازم است.")
        else: scrape_metro(args.city, TARGETS, whole_pages=args.max_pages,
                           district_pages=args.district_pages,
                           max_district_passes=args.max_district_passes,
                           metro_workers=args.metro_workers, stores_file=args.stores_file,
                           baseline_pages=args.baseline_pages)
    if args.action in ("scrape", "both"):
        deep_scrape_city(requests.Session(), CITY_NAME_DEFAULT, TARGETS, args.max_pages,
                         args.district_pages, args.max_district_passes,
                         stores_file=args.stores_file, baseline_pages=args.baseline_pages)
    if args.action in ("export", "both"):
        conn = init_db(DB_PATH)
        export_json(conn, include_outliers=args.include_outliers)
        export_history_json(conn)
        conn.close()