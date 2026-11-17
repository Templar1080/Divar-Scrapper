"""
live_map_server.py — serve real_estate_map.html with live Divar data that refreshes itself.

    pip install -r requirements-live.txt
    python live_map_server.py                         # http://localhost:8000 , refresh every 6 h
    python live_map_server.py --every-hours 3 --radius-km 8 --city "تهران:18"
    python live_map_server.py --demo                  # offline demo (synthetic listings, clearly marked)

What it does
    • collects all commercial rent listings around your stores from Divar (divar_live.py → divar-mcp)
      right after start and then every --every-hours, in a background thread;
    • serves the map at / and the data next to it (data.json, history.json, store files);
    • the map detects the server, shows a «Divar live» status card, reloads the listings by itself when a
      run finishes, and re-scores every store against the fresh listings. A «refresh now» button starts
      a run on demand.

Endpoints
    GET  /                       the map (real_estate_map.html)
    GET  /data.json              listings (map schema)          GET /history.json   daily medians
    GET  /store_comparison.csv   store vs market table           GET /last_run.json  last run summary
    GET  /myli_stores.csv, /hyperstar_stores.csv                 store files from the repo root
    GET  /api/status             collector state (JSON)          POST /api/refresh   start a run now

Run it on a machine that can reach divar.ir (inside Iran, or set DIVAR_MCP_PROXY).
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import divar_live as dl

ROOT = Path(__file__).resolve().parent
MAP_HTML = ROOT / "real_estate_map.html"
log = logging.getLogger("live_map_server")

TYPES = {".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8",
         ".html": "text/html; charset=utf-8"}


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


class Scheduler:
    def __init__(self, cfg: dl.LiveConfig, every_hours: float, demo: bool, initial_run: bool):
        self.cfg, self.every, self.demo = cfg, every_hours * 3600, demo
        self.lock = threading.Lock()
        self.trigger = threading.Event()
        self.state = {"running": False, "phase": None, "done": 0, "total": 0, "message": "",
                      "started_at": None, "next_run_at": None, "last_run": None, "last_error": None}
        last = cfg.out_dir / "last_run.json"
        if last.exists():
            try:
                self.state["last_run"] = json.loads(last.read_text(encoding="utf-8"))
            except ValueError:
                pass
        self.next_run = time.time() if (initial_run or not (cfg.out_dir / "data.json").exists()) \
            else time.time() + self.every
        threading.Thread(target=self.loop, daemon=True, name="divar-collector").start()

    def progress(self, phase, done, total, msg=""):
        with self.lock:
            self.state.update(phase=phase, done=done, total=total, message=msg)

    def request_run(self) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
        self.trigger.set()
        return True

    def loop(self):
        while True:
            wait = max(0.0, self.next_run - time.time())
            with self.lock:
                self.state["next_run_at"] = iso(self.next_run)
            self.trigger.wait(timeout=wait)
            self.trigger.clear()
            with self.lock:
                self.state.update(running=True, phase="start", done=0, total=0, message="",
                                  started_at=iso(time.time()), last_error=None)
            try:
                summary = dl.run_once(self.cfg, self.progress, demo=self.demo)
                err = summary.get("error")
            except Exception as e:  # noqa: BLE001 — keep the server alive whatever happens
                log.error(traceback.format_exc())
                summary, err = None, f"{type(e).__name__}: {e}"
            self.next_run = time.time() + self.every
            with self.lock:
                self.state.update(running=False, phase=None, last_error=err,
                                  next_run_at=iso(self.next_run))
                if summary:
                    self.state["last_run"] = summary
            log.info("run finished: %s", json.dumps(summary or {"error": err}, ensure_ascii=False)[:400])

    def status(self) -> dict:
        data = self.cfg.out_dir / "data.json"
        with self.lock:
            st = dict(self.state)
        st.update(live=True, demo=self.demo, every_hours=self.every / 3600,
                  version=int(data.stat().st_mtime) if data.exists() else 0,
                  radius_km=self.cfg.radius_km, categories=self.cfg.categories,
                  server_time=iso(time.time()))
        return st


def make_handler(sched: Scheduler):
    out = sched.cfg.out_dir
    files = {
        "/data.json": out / "data.json", "/history.json": out / "history.json",
        "/store_comparison.csv": out / "store_comparison.csv", "/last_run.json": out / "last_run.json",
        "/myli_stores.csv": ROOT / "myli_stores.csv", "/hyperstar_stores.csv": ROOT / "hyperstar_stores.csv",
    }
    empty_ok = {"/data.json": b"[]", "/history.json": b"[]"}   # before the first run: empty, not an error

    class Handler(BaseHTTPRequestHandler):
        server_version = "DivarLiveMap/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), TYPES[".json"])

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/real_estate_map.html"):
                return self._send(200, MAP_HTML.read_bytes(), TYPES[".html"])
            if path == "/api/status":
                return self._json(200, sched.status())
            f = files.get(path)
            if f is not None:
                if f.exists():
                    return self._send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
                if path in empty_ok:
                    return self._send(200, empty_ok[path], TYPES[".json"])
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path.split("?", 1)[0] == "/api/refresh":
                started = sched.request_run()
                return self._json(202 if started else 409,
                                  {"started": started, "message": "run started" if started else "already running"})
            self._json(404, {"error": "not found"})

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--every-hours", type=float, default=6.0, help="automatic refresh interval")
    ap.add_argument("--no-initial-run", action="store_true", help="do not collect right after start")
    dl.add_common_args(ap)
    a = ap.parse_args()
    cfg = dl.config_from_args(a)
    sched = Scheduler(cfg, a.every_hours, a.demo, initial_run=not a.no_initial_run)
    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(sched))
    log.info("map: http://%s:%d   (data in %s%s)", a.host, a.port, cfg.out_dir, ", DEMO" if a.demo else "")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
