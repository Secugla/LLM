# -*- coding: utf-8 -*-
"""Rank every coin by similarity to the RAVE / LAB / BTW / LIGHT profile."""
import json, math, os, statistics as st
import bn

BASE = bn.BASE
DATA = os.path.join(BASE, "data")
EX = ["RAVEUSDT", "LABUSDT", "BTWUSDT", "LIGHTUSDT"]

# hard gates: must be a live, liquid, high-volatility, post-60-day market
GATE = dict(n=60, avg_range=12.0, dvol=10.0, med_qv=2.0e6, swing=8.0)

FEATS = [("avg_range", 1.0), ("dvol", 1.0), ("log_qv", 1.0), ("log_burst", 1.0),
         ("log_swing", 1.0), ("maxup", 0.5), ("absdn", 0.5)]


def vec(p):
    return {
        "avg_range": p["avg_range"], "dvol": p["dvol"],
        "log_qv": math.log10(max(p["med_qv"], 1)),
        "log_burst": math.log10(max(p["burst"], 1)),
        "log_swing": math.log10(max(p["swing"], 1)),
        "maxup": p["maxup"], "absdn": abs(p["maxdn"]),
    }


def main():
    recs = json.load(open(os.path.join(DATA, "features.json"), encoding="utf-8"))
    ex = [recs[s] for s in EX if s in recs]
    print("examples:", [p["base"] for p in ex])

    allv = [vec(p) for p in recs.values()]

    # percentile rank of every coin inside the whole pool (0-100), higher = more extreme
    def pct_rank(key, val):
        c = sum(1 for v in allv if v[key] < val)
        return 100.0 * c / (len(allv) - 1)

    W = {"avg_range": 0.25, "dvol": 0.25, "log_burst": 0.15, "log_swing": 0.15,
         "maxup": 0.10, "log_qv": 0.10}

    def score(p):
        v = vec(p)
        return sum(W[k] * pct_rank(k, v[k]) for k in W)

    passed, rejected = [], []
    for sym, p in recs.items():
        ok = (p["n"] >= GATE["n"] and p["avg_range"] >= GATE["avg_range"]
              and p["dvol"] >= GATE["dvol"] and p["med_qv"] >= GATE["med_qv"]
              and p["swing"] >= GATE["swing"])
        p["gate"] = ok
        p["score"] = round(score(p), 1)
        (passed if ok else rejected).append(p)

    passed.sort(key=lambda p: -p["score"])
    print(f"\npass gate: {len(passed)} / {len(recs)}")
    print("example scores:", {p["base"]: p["score"] for p in recs.values() if p["symbol"] in EX})

    print(f"\n{'#':>3} {'coin':12s}{'venue':8s}{'days':>5}{'range%':>7}{'dvol%':>7}{'medQV':>9}"
          f"{'burst':>7}{'swing':>7}{'maxup':>6}{'dd%':>6}{'ret%':>8}{'score':>7}")
    for i, p in enumerate(passed[:50], 1):
        print(f"{i:3d} {p['base']:12s}{p['venue']:8s}{p['n']:5d}{p['avg_range']:7.1f}{p['dvol']:7.1f}"
              f"{p['med_qv']/1e6:8.1f}M{p['burst']:7.0f}{p['swing']:7.1f}{p['maxup']:6.0f}"
              f"{p['dd']:6.0f}{p['ret']:8.0f}{p['score']:7.1f}")

    json.dump({"examples": EX, "gate": GATE, "weights": W, "selected": passed,
               "pool": len(recs), "rejected": len(rejected)},
              open(os.path.join(DATA, "selection.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("\nsaved selection.json")


if __name__ == "__main__":
    main()
