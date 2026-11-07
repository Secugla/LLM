# -*- coding: utf-8 -*-
"""Compute the post-listing (day 60 -> today) behavioural profile for every coin."""
import json, math, os, statistics as st, time
import bn

BASE = bn.BASE
DATA = os.path.join(BASE, "data")
OLD = r"C:\Users\86180\Desktop\AI_pro\screener\report_data.json"
EX = ["RAVEUSDT", "LABUSDT", "BTWUSDT", "LIGHTUSDT"]


def prof(rows, cut_ms):
    post = [r for r in rows if r[0] >= cut_ms]
    if len(post) < 30:
        return None
    c = [r[4] for r in post]
    h = [r[2] for r in post]
    l = [r[3] for r in post]
    q = [r[5] for r in post]
    rng = [(a - b) / x * 100 for a, b, x in zip(h, l, c)]
    ret = [(c[i] / c[i - 1] - 1) * 100 for i in range(1, len(c))]
    med = st.median(q)
    return dict(
        n=len(post),
        ret=round((c[-1] / c[0] - 1) * 100, 1),
        avg_range=round(st.mean(rng), 2),
        dvol=round(st.pstdev(ret), 2),
        med_qv=med,
        burst=round(max(q) / max(med, 1.0), 1),
        swing=round(max(c) / min(c), 2),
        maxup=round(max(ret), 1),
        maxdn=round(min(ret), 1),
        price=c[-1],
        dd=round((c[-1] / max(h) - 1) * 100, 1),
        ath_mult=round(max(h) / c[0], 2),
        from_ms=post[0][0],
        to_ms=post[-1][0],
    )


def main():
    recs = {}

    # ---- pool A: the 139 coins that trade on Binance spot
    old = json.load(open(OLD, encoding="utf-8"))
    A = old["main"]
    print("pool A:", len(A))
    for i, r in enumerate(A):
        sym = r["symbol"]
        rows = [[x[0], x[1], x[2], x[3], x[4], x[6]] for x in bn.spot_series(sym)]
        if not rows:
            continue
        p = prof(rows, rows[0][0] + 60 * 86400000)
        if p:
            p.update(symbol=sym, base=r["base"], venue="spot", listing_ms=rows[0][0],
                     spot_max=r["spot_max"], fut_max=r["fut_max"])
            recs[sym] = p
        if (i + 1) % 40 == 0:
            print("  A", i + 1, flush=True)
    print("pool A done:", len(recs))

    # ---- pool B: perp-only futures symbols
    uni = json.load(open(os.path.join(DATA, "universe.json"), encoding="utf-8"))
    print("universe symbols:", len(uni["symbols"]))
    for i, (sym, v) in enumerate(uni["symbols"].items()):
        rows = v["rows"]
        if len(rows) < 90:
            continue
        p = prof(rows, rows[0][0] + 60 * 86400000)
        if p:
            p.update(symbol=sym, base=sym.replace("USDT", ""), venue="futures",
                     listing_ms=rows[0][0], spot_max=0.0,
                     fut_max=max(x[5] for x in rows))
            recs[sym] = p
    print("total records:", len(recs))

    json.dump(recs, open(os.path.join(DATA, "features.json"), "w", encoding="utf-8"))

    # ---- distribution report
    print("\n=== examples ===")
    hdr = f"{'coin':14s}{'n':>4}{'ret%':>9}{'range%':>8}{'dvol%':>7}{'medQV':>9}{'burst':>8}{'swing':>8}{'maxup':>7}{'maxdn':>7}{'dd%':>7}"
    print(hdr)
    for s in EX:
        p = recs.get(s)
        if not p:
            print(s, "missing"); continue
        print(f"{p['base']:14s}{p['n']:4d}{p['ret']:9.0f}{p['avg_range']:8.1f}{p['dvol']:7.1f}"
              f"{p['med_qv']/1e6:8.1f}M{p['burst']:8.0f}{p['swing']:8.1f}{p['maxup']:7.0f}{p['maxdn']:7.0f}{p['dd']:7.0f}")

    print("\n=== pool quantiles ===")
    for k in ["ret", "avg_range", "dvol", "med_qv", "burst", "swing", "maxup", "dd"]:
        v = sorted(x[k] for x in recs.values())
        qs = [v[int(len(v) * p)] for p in (0.1, 0.25, 0.5, 0.75, 0.9)]
        if k == "med_qv":
            qs = [f"{x/1e6:.1f}M" for x in qs]
            print(f"  {k:10s}", qs)
        else:
            print(f"  {k:10s}", [round(x, 1) for x in qs])

    # how many pass a naive profile filter
    ex = [recs[s] for s in EX if s in recs]
    print("\nexample means: range %.1f dvol %.1f medQV %.1fM burst %.0f swing %.1f" % (
        st.mean(p["avg_range"] for p in ex), st.mean(p["dvol"] for p in ex),
        st.mean(p["med_qv"] for p in ex) / 1e6, st.mean(p["burst"] for p in ex),
        st.mean(p["swing"] for p in ex)))


if __name__ == "__main__":
    main()
