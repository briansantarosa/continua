"""newsie.py — the newspaper rack (facts she pulls, never pushed).

Plan: newsieplan.md (the designer approved in principle 2026-09-26). House law it
implements: PULL-NOT-PUSH (house ruling 09-26) — facts are a library she
visits, never a feed pushed into her mind. Nothing scheduled into her
context, ever; this service only makes the rack AVAILABLE. She pulls via
the `news` house tool when curious; availability is the whole feature.

Shape: a small localhost service in the Searchie pattern.
  - Port 21001, bound to 127.0.0.1 ONLY. The bridge consumes it via the
    `news` house tool (main process) — the same path as Searchie. The
    desk sandbox denies loopback BY DESIGN: exploratory desk code can never
    call newsie directly; promoted code runs as house (toolplan.md).
  - Kill switch: CONTINUA_NEWSIE=0 (API answers 503; fetch loop stands
    down). Fail-open everywhere: a broken feed is a log line, never a
    crash of the residents' chat paths.
  - Storage: SQLite at CONTINUA_NEWSIE_DB (default <repo>/data/newsie.db);
    items table + feeds_state (conditional-GET politeness state).
  - Parser: stdlib xml.etree (RSS 2.0 + Atom). Fetch via httpx (the house
    client). Politeness: per-feed interval (default 3h), conditional GET
    (ETag/Last-Modified), one retry, per-host timeout. We fetch like a
    guest.
  - Provenance rule (adopted from Yuralume's lesson, house law): never a
    clipped summary without its original link — every item carries source +
    url + published.

Explicitly NOT building (plan): push of any kind (no scheduled injection,
no briefing prompt-stuffing, nothing in wakes), auto-summarization into her
memory (news is not memory — she clips what she wants with her shell),
API-keyed sources in v1.

v1.1 deferred: /holidays, /onthisday.
"""

import json
import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] newsie: %(message)s")
logger = logging.getLogger("continua.newsie")

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 21001
DEFAULT_DB = os.path.join(BASE, "data", "newsie.db")
# NOTE (2026-09-27 live find): configs/*.yaml is the BRIDGE'S bot-config
# namespace — a tokenless yaml there stalls its boot scan. The feeds
# live at the repo root instead; CONTINUA_NEWSIE_FEEDS overrides.
DEFAULT_FEEDS = os.path.join(BASE, "feeds.yaml")
KILL_SWITCH_ENV = "CONTINUA_NEWSIE"
DEFAULT_INTERVAL_H = 3
WEATHER_CACHE_S = 30 * 60


def enabled() -> bool:
    """Kill switch: CONTINUA_NEWSIE=0 stands the whole rack down."""
    return os.environ.get(KILL_SWITCH_ENV, "1") != "0"


def db_path() -> str:
    return os.getenv("CONTINUA_NEWSIE_DB", DEFAULT_DB)


# --------------------------------------------------------------------------
# Parsing — pure functions over bytes; tests run on fixture strings, the
# network is never involved in a parse.
# --------------------------------------------------------------------------

def _clean(text) -> str:
    """Tag-strip + entity-decode + whitespace-collapse, one honest pass."""
    import html
    import re
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return " ".join(html.unescape(s).split())


def _root_tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1].lower()


def parse_rss(root, source: str, category: str) -> list:
    """RSS 2.0 / RDF-ish channel+item shape. Every item carries its
    provenance triple (source, url, published) — the house rule."""
    out = []
    for item in root.iter():
        if _root_tag(item) != "item":
            continue
        title = _clean(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        summary = _clean(item.findtext("description") or "")[:500]
        pub = (item.findtext("pubDate") or "").strip()
        out.append({"source": source, "url": link, "title": title,
                    "summary": summary, "published": pub,
                    "category": category})
    return out


def parse_atom(root, source: str, category: str) -> list:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in root.findall("a:entry", ns):
        title = _clean(entry.findtext("a:title", default="", namespaces=ns))
        lel = entry.find("a:link", ns)
        link = (lel.get("href") or "").strip() if lel is not None else ""
        if not title or not link:
            continue
        summary = _clean(entry.findtext("a:summary", default="", namespaces=ns)
                         or entry.findtext("a:content", default="", namespaces=ns))[:500]
        pub = (entry.findtext("a:updated", default="", namespaces=ns)
               or entry.findtext("a:published", default="", namespaces=ns)).strip()
        out.append({"source": source, "url": link, "title": title,
                    "summary": summary, "published": pub,
                    "category": category})
    return out


def parse_feed(data: bytes, source: str, category: str) -> list:
    """RSS 2.0 / Atom, sniffed by root tag. Unparseable or unknown → []
    (fail-open: a broken feed is a log line, never a crash)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        logger.warning("[Newsie] feed %s unparseable: %s", source, e)
        return []
    tag = _root_tag(root)
    if tag in ("rss", "rdf"):
        return parse_rss(root, source, category)
    if tag == "feed":
        return parse_atom(root, source, category)
    logger.warning("[Newsie] feed %s: unknown root tag %r", source, tag)
    return []


# --------------------------------------------------------------------------
# Storage — SQLite. url UNIQUE is the dedupe key; fetched_at is ours.
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  source TEXT NOT NULL,
  url TEXT UNIQUE,
  title TEXT,
  summary TEXT DEFAULT '',
  category TEXT DEFAULT '',
  published TEXT DEFAULT '',
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_cat ON items(category);
CREATE TABLE IF NOT EXISTS feeds_state(
  name TEXT PRIMARY KEY,
  etag TEXT,
  last_modified TEXT,
  last_fetch REAL DEFAULT 0
);
"""


def connect(path: str = None):
    # check_same_thread=False: the rack is multi-threaded by design — the
    # poll loop and per-request handler threads share one conn, serialized
    # by short transactions (this is a tiny localhost reader, not a bank).
    p = path or db_path()
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    except OSError as e:
        logger.warning("[Newsie] db dir create failed: %s", e)
    conn = sqlite3.connect(p, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def store_items(conn, items: list, now: str = None) -> int:
    """Insert items (url-unique). Returns how many were NEW — the poll
    summary's 'new' and the dedupe tests both read this."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for it in items:
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO items(source,url,title,summary,"
                    "category,published,fetched_at) VALUES(?,?,?,?,?,?,?)",
                    (it.get("source", ""), it.get("url", ""),
                     it.get("title", ""), it.get("summary", ""),
                     it.get("category", ""), it.get("published", ""), now))
                n += cur.rowcount
        except sqlite3.Error as e:
            logger.warning("[Newsie] item insert failed (fail-open): %s", e)
    return n


def headlines(conn, cat: str = None, limit: int = 5) -> list:
    """Newest first, ALWAYS with source+url+published (provenance rule:
    never a clipped summary without its original link)."""
    q = ("SELECT source,url,title,summary,category,published,fetched_at "
         "FROM items")
    args = []
    if cat and cat != "all":
        q += " WHERE category = ?"
        args.append(cat)
    q += " ORDER BY rowid DESC LIMIT ?"
    args.append(max(1, min(int(limit or 5), 20)))
    try:
        return [dict(r) for r in conn.execute(q, args)]
    except sqlite3.Error as e:
        logger.warning("[Newsie] headlines query failed (fail-open): %s", e)
        return []


# --------------------------------------------------------------------------
# Fetching — a polite guest. Conditional GET, per-feed interval, one retry.
# fetch_fn is injectable so tests exercise the full poll path with zero
# network (house test convention: agent-free, net-free).
# --------------------------------------------------------------------------

def _fetch(url: str, etag: str = "", last_modified: str = "",
           timeout: float = 10.0) -> tuple:
    """One conditional GET → (status, body, etag, last_modified);
    status ∈ {'ok','not-modified','error'}."""
    import httpx
    headers = {"User-Agent": "continua-newsie/1.0 (polite house fetcher)"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as s:
            r = s.get(url, headers=headers)
        if r.status_code == 304:
            return ("not-modified", b"", etag, last_modified)
        r.raise_for_status()
        return ("ok", r.content, r.headers.get("ETag", ""),
                r.headers.get("Last-Modified", ""))
    except Exception as e:
        logger.warning("[Newsie] fetch %s failed: %s",
                       urlparse(url).netloc, e)
        return ("error", b"", etag, last_modified)


def fetch_with_retry(url: str, etag: str = "", last_modified: str = "",
                     attempts: int = 2) -> tuple:
    """Per-host timeout inside _fetch; ONE retry, short backoff. A
    persistently-broken feed is a log line, never a crash."""
    st, body, e, lm = "error", b"", etag, last_modified
    for i in range(attempts):
        st, body, e, lm = _fetch(url, etag, last_modified)
        if st in ("ok", "not-modified"):
            return (st, body, e, lm)
        if i < attempts - 1:
            time.sleep(1.0)
    return (st, body, e, lm)


def load_feeds(path: str = None) -> list:
    """configs/newsie.yaml feeds. Fail-open to []. Re-read EVERY poll
    cycle — no restarts to add a feed (the designer edits the yaml)."""
    import yaml
    path = path or os.getenv("CONTINUA_NEWSIE_FEEDS", DEFAULT_FEEDS)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return [f for f in (cfg.get("feeds") or [])
                if isinstance(f, dict) and f.get("url")]
    except OSError as e:
        logger.warning("[Newsie] feeds config unreadable (fail-open): %s", e)
        return []


def poll_once(conn, feeds: list, now: float = None, force: bool = False,
              fetch_fn=None) -> dict:
    """One poll pass over all feeds; per-feed due-ness from feeds_state
    (interval_h, default 3h). force=True polls everything (fetch-once CLI,
    tests). Returns {fetched, skipped, errors, new}."""
    fetch_fn = fetch_fn or fetch_with_retry
    now = time.time() if now is None else now
    conn.row_factory = None  # state read uses tuples
    state = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT name,etag,last_modified,last_fetch FROM feeds_state")}
    conn.row_factory = sqlite3.Row
    summary = {"fetched": 0, "skipped": 0, "errors": 0, "new": 0}
    for feed in feeds:
        name = str(feed.get("name") or "?")
        url = str(feed.get("url") or "")
        if not url:
            continue
        etag, lm, last = state.get(name, ("", "", 0.0))
        interval_s = float(feed.get("interval_h", DEFAULT_INTERVAL_H)) * 3600
        if not force and (now - (last or 0.0)) < interval_s:
            summary["skipped"] += 1
            continue
        st, body, netag, nlm = fetch_fn(url, etag, lm)
        if st == "ok":
            items = parse_feed(body, name, str(feed.get("cat", "")))
            summary["new"] += store_items(conn, items)
            summary["fetched"] += 1
        elif st == "not-modified":
            summary["skipped"] += 1
        else:
            summary["errors"] += 1
        stamp = now if now is not None else time.time()
        with conn:
            conn.execute(
                "INSERT INTO feeds_state(name,etag,last_modified,last_fetch)"
                " VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "etag=excluded.etag, last_modified=excluded.last_modified,"
                " last_fetch=excluded.last_fetch",
                (name, netag, nlm, now if now is not None else time.time()))
    return summary


def _fetch_conditional(url: str, etag: str = "", last_modified: str = "",
                       attempts: int = 2) -> tuple:
    """Conditional-GET wrapper with ONE retry (politeness valve)."""
    st, body, e, lm = "error", b"", etag, last_modified
    for i in range(attempts):
        st, body, e, lm = _fetch(url, etag, lm)
        if st in ("ok", "not-modified"):
            return (st, body, e, lm)
        if i < attempts - 1:
            time.sleep(1.0)
    return (st, body, e, lm)


# --------------------------------------------------------------------------
# /article — lite readability, stdlib only.
# --------------------------------------------------------------------------

def extract_article(html: str) -> dict:
    """Lite readability: <title> + paragraph text, tags stripped. She can
    always hand the URL to Searchie for a deep read — this is quick."""
    import re
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    title = _clean(m.group(1)) if m else ""
    body = re.sub(r"(?is)<(script|style|nav|footer|header|noscript|form)"
                  r"[^>]*>.*?</\1>", " ", html or "")
    paras = []
    for m in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", body):
        p = _clean(m.group(1))
        if len(p) >= 40:
            paras.append(p)
        if len(paras) >= 40:
            break
    if not paras and title:
        fallback = _clean(body)[:2000]
        if fallback:
            paras = [fallback]
    return {"title": title, "paragraphs": paras,
            "text": "\n\n".join(paras)[:6000]}


# --------------------------------------------------------------------------
# /weather — Open-Meteo, keyless, cached 30 min (plan). Pure data, no keys.
# --------------------------------------------------------------------------

_weather_cache = {"key": None, "ts": 0.0, "payload": None}
_WMO = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy",
        3: "overcast", 45: "fog", 45.0: "fog", 48: "rime fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow",
        80: "rain showers", 81: "heavy showers", 95: "thunderstorm"}


def weather(lat: float, lon: float, now: float = None,
            fetch_fn=None) -> dict:
    """Open-Meteo current conditions, keyless, 30-min cache. Fail-open to
    an honest error — never a fabricated forecast."""
    now = time.time() if now is None else now
    key = (round(float(lat), 3), round(float(lon), 3))
    if (_weather_cache["payload"] and _weather_cache["key"] == key
            and now - _weather_cache["ts"] < WEATHER_CACHE_S):
        return _weather_cache["payload"]
    def _default(url):
        st, body, _, _ = _fetch(url)
        return body if st == "ok" else b""
    raw = (fetch_fn or _default)(f"https://api.open-meteo.com/v1/forecast"
                                 f"?latitude={key[0]}&longitude={key[1]}"
                                 "&current=temperature_2m,weather_code,"
                                 "wind_speed_10m&timezone=auto")
    try:
        data = json.loads(raw)
        cur = data.get("current") or {}
        payload = {"temperature_c": cur.get("temperature_2m"),
                   "wind_kmh": cur.get("wind_speed_10m"),
                   "conditions": _WMO.get(cur.get("weather_code"),
                                          f"code {cur.get('weather_code')}"),
                   "lat": key[0], "lon": key[1],
                   "source": "api.open-meteo.com"}
        _weather_cache.update(key=key, ts=now, payload=payload)
        return payload
    except Exception as e:
        logger.warning("[Newsie] weather failed (fail-open): %s", e)
        return {"error": f"weather unavailable: {e}"}


# --------------------------------------------------------------------------
# /moon — pure math, no network, no keys. Her correspondent is named
# "new moon"; the rack answers moon questions locally, always available.
# --------------------------------------------------------------------------

_SYNODIC_DAYS = 29.530588853
_KNOWN_NEW_MOON_EPOCH = 947182440.0  # 2000-01-06 18:14:00 UTC
_PHASE_NAMES = ["new moon", "waxing crescent", "first quarter",
                "waxing gibbous", "full moon", "waning gibbous",
                "last quarter", "waning crescent"]


def moon_phase(now: float = None) -> dict:
    """phase 0..1 (0=new, 0.5=full) + name + age_days. Computed locally —
    pure math, no network, no keys (plan: capability 'none')."""
    now = time.time() if now is None else now
    age_days = ((now - _KNOWN_NEW_MOON_EPOCH) / 86400.0) % _SYNODIC_DAYS
    phase = age_days / _SYNODIC_DAYS
    return {"phase": round(phase, 4),
            "name": _PHASE_NAMES[int(phase * 8 + 0.5) % 8],
            "age_days": round(age_days, 1),
            "computed": "locally, pure math — no network"}


# --------------------------------------------------------------------------
# The rack — poll thread + HTTP server, bound to 127.0.0.1 only.
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    conn = None  # injected by serve()

    def log_message(self, fmt, *args):
        logger.debug("[Newsie] " + fmt, *args)

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if not os.environ.get(KILL_SWITCH_ENV, "1") != "0" and False:
                pass  # (checked per-endpoint below — env read is cheap)
            if os.environ.get(KILL_SWITCH_ENV, "") == "0":
                self._json(503, {"error": "newsie is switched off "
                                          f"({KILL_SWITCH_ENV}=0)"})
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/health":
                self._json(200, {"ok": True, "service": "newsie"})
            elif u.path == "/headlines":
                cat = (q.get("cat") or "all").strip().lower()
                try:
                    limit = int(q.get("limit") or 5)
                except ValueError:
                    limit = 5
                items = headlines(self.conn, cat=cat, limit=limit)
                for it in items:  # provenance rule: never a naked summary
                    it.setdefault("url", "")
                    it.setdefault("source", "")
                    it.setdefault("published", "")
                self._json(200, {"cat": cat, "count": len(items),
                                 "items": items})
            elif u.path == "/article":
                url = (q.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    self._json(400, {"error": "url must be http(s)"})
                    return
                st, body, _, _ = _fetch(url, timeout=20.0)
                if st != "ok":
                    self._json(502, {"error": f"fetch failed ({st})"})
                    return
                art = extract_article(
                    body.decode("utf-8", errors="replace"))
                art["url"] = url  # provenance travels with the deep read
                self._json(200, art)
            elif u.path == "/weather":
                try:
                    lat, lon = float(q.get("lat")), float(q.get("lon"))
                except (TypeError, ValueError):
                    self._json(400, {"error": "lat and lon required"})
                    return
                self._json(200, weather(lat, lon))
            elif u.path == "/moon":
                self._json(200, moon_phase())
            else:
                self._json(404, {"error": "unknown endpoint",
                                 "endpoints": ["/headlines", "/article",
                                               "/weather", "/moon",
                                               "/health"]})
        except Exception as e:
            logger.warning("[Newsie] handler error (fail-open): %s", e)
            try:
                self._json(500, {"error": str(e)[:200]})
            except Exception:
                pass


def serve(host: str = "127.0.0.1", port: int = None,
          poll_interval_s: int = 300):
    """The rack: poll loop (daemon thread) + HTTP server (main thread).
    Feeds config re-read every cycle; kill switch checked per poll."""
    os.makedirs(os.path.dirname(db_path()) or ".", exist_ok=True)
    conn = connect()
    Handler.conn = conn  # the handler threads read the rack through this

    def poll_loop():
        while True:
            try:
                if enabled():
                    s = poll_once(conn, load_feeds())  # re-read each cycle
                    logger.info("[Newsie] poll: %s", s)
                else:
                    logger.info("[Newsie] kill switch — standing down")
                    time.sleep(3600)
                    continue
            except Exception:
                logger.warning("[Newsie] poll cycle failed (fail-open)",
                               exc_info=True)
            time.sleep(300)

    import threading
    threading.Thread(target=poll_loop, daemon=True).start()

    port = port or DEFAULT_PORT
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    logger.info("[Newsie] rack open on http://%s:%s (kill switch %s=0)",
                host, port, KILL_SWITCH_ENV)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    import argparse
    ap = argparse.ArgumentParser(description="newsie — the newspaper rack")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--fetch-once", action="store_true",
                    help="one poll pass (all feeds, force), then exit")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    if a.fetch_once:
        conn = connect(a.db)
        feeds = load_feeds()
        if not feeds:
            logger.warning("no feeds configured — nothing to fetch")
        print(json.dumps(poll_once(conn, feeds, force=True)))
        return
    serve(port=a.port)


if __name__ == "__main__":
    main()
