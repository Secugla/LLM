# -*- coding: utf-8 -*-
"""
Discover the Binance USDⓈ-M futures universe (including perp-only symbols that have
no Binance spot market) and cache a full daily series for every symbol listed
within the last 3 years.

Name discovery: Gate.io USDT-perp + Gate.io USDT-spot bases (public lists),
minus every base that already trades on Binance spot.  Binance gives no public
symbol-list endpoint that is reachable here, so a name pool is required; this is
documented as a coverage limitation.
"""
import json, os, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
import bn

BASE = bn.BASE
OUT = os.path.join(BASE, "data", "universe.json")
HDR = {"User-Agent": "Mozilla/5.0 (research)"}


def gate_names():
    perp, spot = set(), set()
    for url, key in (("https://api.gateio.ws/api/v4/futures/usdt/contracts", "perp"),
                     ("https://api.gateio.ws/api/v4/spot/currency_pairs", "spot")):
        with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=60) as r:
            d = json.load(r)
        if key == "perp":
            for c in d:
                n = c.get("name", "")
                if n.endswith("_USDT"):
                    perp.add(n[:-5])
        else:
            for p in d:
                if p.get("quote") == "USDT":
                    spot.add(p.get("base"))
    return perp, spot


def binance_spot_bases():
    u = "https://data-api.binance.vision/api/v3/exchangeInfo"
    with urllib.request.urlopen(urllib.request.Request(u, headers=HDR), timeout=60) as r:
        info = json.load(r)
    return {s["baseAsset"] for s in info["symbols"] if s["quoteAsset"] == "USDT"}


def main():
    perp, gspot = gate_names()
    bspot = binance_spot_bases()
    cands = sorted((perp | gspot) - bspot)
    cands = [c for c in cands if c and c.isascii() and len(c) <= 15]
    print(f"gate perp={len(perp)} gate spot={len(gspot)} binance spot={len(bspot)} -> candidates {len(cands)}")

    # 1) does a Binance futures market exist at all, and since when
    def probe(base):
        sym = base + "USDT"
        try:
            first, _ = bn.find_first_month(sym, lo="2023-09")
        except Exception:
            return None
        return (sym, first) if first else None

    t0 = time.time()
    found = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, res in enumerate(ex.map(probe, cands)):
            if res:
                found.append(res)
            if (i + 1) % 200 == 0:
                print(f"  probed {i+1}/{len(cands)}  found={len(found)}  {time.time()-t0:.0f}s", flush=True)
    print(f"probe done: {len(found)} futures symbols, {time.time()-t0:.0f}s")

    # 2) keep only those that started within the 3-year window
    recent = [(s, m) for s, m in found if m >= "2023-10"]
    print(f"listed since 2023-10: {len(recent)}")

    # 3) pull full series
    def series(item):
        sym, first = item
        try:
            rows = bn.fut_series(sym, first)
        except Exception:
            rows = []
        return sym, first, [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in rows]

    data = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, (sym, first, rows) in enumerate(ex.map(series, recent)):
            if rows:
                data[sym] = {"first": first, "rows": rows}
            if (i + 1) % 50 == 0:
                print(f"  series {i+1}/{len(recent)}  {time.time()-t0:.0f}s", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"built": time.strftime("%Y-%m-%d %H:%M"),
                   "candidates": len(cands), "symbols": data}, f)
    print("saved", OUT, len(data), "symbols")
    old = [s for s, m in found if m < "2023-10"]
    print("pre-2023 symbols skipped:", len(old))


if __name__ == "__main__":
    main()
