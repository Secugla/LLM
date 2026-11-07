# -*- coding: utf-8 -*-
"""Shared helpers: Binance USDⓈ-M futures + spot daily klines, cached on disk."""
import io, json, os, time, urllib.request, urllib.error, zipfile, calendar
from concurrent.futures import ThreadPoolExecutor

BASE = r"C:\Users\86180\Desktop\LLM\Dpseek"
CACHE = os.path.join(BASE, "data", "fut")
SCACHE = os.path.join(BASE, "data", "spot")
os.makedirs(CACHE, exist_ok=True)
os.makedirs(SCACHE, exist_ok=True)
HDR = {"User-Agent": "Mozilla/5.0 (research)"}
FUT_M = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/1d/{s}-1d-{m}.zip"
FUT_D = "https://data.binance.vision/data/futures/um/daily/klines/{s}/1d/{s}-1d-{d}.zip"
SPOT = "https://data-api.binance.vision/api/v3/klines"


def month_keys(start_ym, end_ym):
    """inclusive list of 'YYYY-MM' between two month strings"""
    out = []
    y, m = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _get(url, timeout=40):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503, 502):
                time.sleep(2 + i * 4); continue
            return None
        except Exception:
            time.sleep(1.5 + i * 3)
    return None


def fut_month(sym, mon):
    """rows of one monthly dump (list of dicts) or None if that month does not exist"""
    fp = os.path.join(CACHE, f"{sym}_{mon}.json")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    if os.path.exists(fp + ".404"):
        return None
    b = _get(FUT_M.format(s=sym, m=mon))
    if b is None:
        open(fp + ".404", "w").close()
        return None
    rows = []
    try:
        z = zipfile.ZipFile(io.BytesIO(b))
        txt = z.read(z.namelist()[0]).decode()
    except Exception:
        txt = ""
    for line in txt.strip().split("\n"):
        p = line.split(",")
        if len(p) < 8 or p[0] == "open_time":
            continue
        rows.append([int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                     float(p[5]), float(p[7])])
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return rows


def fut_day(sym, ds):
    fp = os.path.join(CACHE, f"{sym}_D{ds}.json")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    if os.path.exists(fp + ".404"):
        return None
    b = _get(FUT_D.format(s=sym, d=ds))
    if b is None:
        open(fp + ".404", "w").close()
        return None
    rows = []
    try:
        z = zipfile.ZipFile(io.BytesIO(b))
        txt = z.read(z.namelist()[0]).decode()
    except Exception:
        txt = ""
    for line in txt.strip().split("\n"):
        p = line.split(",")
        if len(p) < 8 or p[0] == "open_time":
            continue
        rows.append([int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                     float(p[5]), float(p[7])])
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return rows


def find_first_month(sym, lo="2023-09", hi=None):
    """binary search the first month that has a dump -> (month_str|None, probes)"""
    hi = hi or time.strftime("%Y-%m", time.gmtime())
    months = month_keys(lo, hi)
    # the current month's monthly dump is only published after month end
    probes = 0
    while months and fut_month(sym, months[-1]) is None and probes < 2:
        months.pop()
        probes += 1
    if not months:
        return None, probes
    if fut_month(sym, months[-1]) is None:
        return None, probes + 1      # not trading now -> treat as absent
    probes += 1
    if fut_month(sym, months[0]) is not None:
        return months[0], probes + 1     # older than the search window
    lo_i, hi_i = 0, len(months) - 1
    probes += 1
    while hi_i - lo_i > 1:
        mid = (lo_i + hi_i) // 2
        probes += 1
        if fut_month(sym, months[mid]) is None:
            lo_i = mid
        else:
            hi_i = mid
    return months[hi_i], probes


def fut_series(sym, start_mon, end_ds=None):
    """all daily rows from start_mon to now (monthly dumps + daily fallback for the current month)"""
    now = time.gmtime()
    end_ds = end_ds or time.strftime("%Y-%m-%d", now)
    cur = time.strftime("%Y-%m", now)
    out = {}
    for mon in month_keys(start_mon, cur):
        rows = fut_month(sym, mon)
        if rows is not None:
            for r in rows:
                out[r[0]] = r
            continue
        # month not published yet -> daily files for the days we can get
        y, m = int(mon[:4]), int(mon[5:7])
        for d in range(1, calendar.monthrange(y, m)[1] + 1):
            ds = f"{mon}-{d:02d}"
            if ds > end_ds:
                break
            dr = fut_day(sym, ds)
            if dr:
                for r in dr:
                    out[r[0]] = r
    return [out[k] for k in sorted(out)]


def spot_series(sym, limit=1000):
    fp = os.path.join(SCACHE, sym + ".json")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = None
        url = f"{SPOT}?symbol={sym}&interval=1d&startTime=1&limit={limit}"
        b = _get(url, timeout=30)
        if b:
            raw = json.loads(b)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(raw, f)
    if not raw:
        return []
    return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
             float(k[5]), float(k[7])] for k in raw]


def pmap(fn, items, workers=12):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))
