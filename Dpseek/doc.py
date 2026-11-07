# -*- coding: utf-8 -*-
"""Build the document: charts + HTML report."""
import json, math, os, statistics as st, time
import bn

BASE = bn.BASE
DATA = os.path.join(BASE, "data")
CH = os.path.join(BASE, "charts")
os.makedirs(CH, exist_ok=True)
THRESH = 10_000_000.0
EX = ["RAVEUSDT", "LABUSDT", "BTWUSDT", "LIGHTUSDT"]
DAY = 86400000

INK, MUT, GRID = "#111827", "#6b7280", "#e5e7eb"
GREEN, RED, ORANGE, BLUE = "#16a34a", "#dc2626", "#ea8a1f", "#2563eb"
FONT = "Segoe UI,Microsoft YaHei,PingFang SC,sans-serif"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(v):
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e8: return f"${v/1e6:.0f}M"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def price_fmt(v):
    if v >= 1000: return f"{v:,.0f}"
    if v >= 100: return f"{v:.1f}"
    if v >= 1: return f"{v:.3f}"
    if v >= 0.01: return f"{v:.4f}"
    if v >= 0.0001: return f"{v:.6f}"
    return f"{v:.8f}".rstrip("0")


def d2s(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


class W:
    def __init__(self, w, h):
        self.w, self.h, self.p = w, h, []

    def add(self, s): self.p.append(s)

    def text(self, x, y, s, size=9, anchor="middle", fill=INK, weight="400", op=1.0):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
                 f'text-anchor="{anchor}" fill="{fill}" font-weight="{weight}" opacity="{op}">{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, stroke=GRID, wid=1, dash=None, op=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" '
                 f'stroke-width="{wid}"{d} opacity="{op}"/>')

    def rect(self, x, y, w, h, fill, rx=0, op=1.0):
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0.6):.1f}" height="{max(h,0.6):.1f}" '
                 f'rx="{rx}" fill="{fill}" opacity="{op}"/>')

    def path(self, d, stroke=INK, wid=1, fill="none", op=1.0):
        self.add(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{wid}" opacity="{op}" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')

    def circle(self, x, y, r, fill, op=1.0, stroke="none", sw=1):
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{fill}" opacity="{op}" '
                 f'stroke="{stroke}" stroke-width="{sw}"/>')

    def out(self, label):
        return (f'<svg viewBox="0 0 {self.w} {self.h}" width="100%" role="img" aria-label="{esc(label)}" '
                f'xmlns="http://www.w3.org/2000/svg">' + "".join(self.p) + "</svg>")


# ---------------------------------------------------------------- data
def load_rows(rec):
    sym = rec["symbol"]
    if rec["venue"] == "spot":
        return [[x[0], x[1], x[2], x[3], x[4], x[6]] for x in bn.spot_series(sym)]
    uni = load_rows.uni
    return uni[sym]["rows"]


def aggregate(rows, gran):
    """gran: 'd' | 'w' | 'm' -> list of [t, o, h, l, c, avg_qv, ndays]"""
    if gran == "d":
        return [[r[0], r[1], r[2], r[3], r[4], r[5], 1] for r in rows]
    out, key, cur = [], None, None
    for r in rows:
        tm = time.gmtime(r[0] / 1000)
        if gran == "w":
            k = time.strftime("%Y-%W", tm)
        else:
            k = time.strftime("%Y-%m", tm)
        if k != key:
            if cur:
                out.append(cur)
            key, cur = k, [r[0], r[1], r[2], r[3], r[4], r[5], 1]
        else:
            cur[2] = max(cur[2], r[2]); cur[3] = min(cur[3], r[3]); cur[4] = r[4]
            cur[5] += r[5]; cur[6] += 1
    if cur:
        out.append(cur)
    for b in out:
        b[5] = b[5] / b[6]
    return out


def pick_gran(n_days):
    if n_days <= 95: return "d", "日"
    if n_days <= 460: return "w", "周"
    return "m", "月"


def chart(rec, rows_win, gran_label, title_note, w=560, h=340):
    base = rec["base"]
    bars = aggregate(rows_win, pick_gran(len(rows_win))[0])
    n = len(bars)
    cw, chh = w, h
    ml, mr = 46, 10
    py0, py1 = 46, h - 108
    vy0, vy1 = h - 96, h - 34
    pw = cw - ml - mr
    g = W(cw, chh)

    c = [b[4] for b in bars]
    hi = max(b[2] for b in bars)
    lo = min(b[3] for b in bars)
    use_log = hi / lo > 2.5 if lo > 0 else False
    if use_log:
        plo = math.log10(lo) - 0.05
        phi = math.log10(hi) + 0.05
        tf = lambda v: (math.log10(max(v, 1e-12)) - plo) / (phi - plo)
    else:
        pad = (hi - lo) * 0.08 or hi * 0.05
        plo, phi = lo - pad, hi + pad
        tf = lambda v: (v - plo) / (phi - plo)
    PY = lambda v: min(max(py1 - tf(v) * (py1 - py0), py0 + 0.5), py1 - 0.5)

    q = [b[5] for b in bars]
    vmax = max(max(q), THRESH * 1.4)
    vmin = max(min([x for x in q if x > 0] or [1e5]), 1e4)
    vlo, vhi = math.log10(vmin * 0.55), math.log10(vmax * 1.3)
    VYraw = lambda v: vy1 - ((math.log10(max(v, 1e-9)) - vlo) / (vhi - vlo)) * (vy1 - vy0)
    VY = lambda v: min(max(VYraw(v), vy0 + 0.4), vy1)

    g.rect(ml, py0, pw, py1 - py0, "#fcfdfe")
    g.rect(ml, vy0, pw, vy1 - vy0, "#fcfdfe")

    for f in (0.25, 0.5, 0.75):
        yy = py1 - f * (py1 - py0)
        g.line(ml, yy, ml + pw, yy, GRID, 1, None, 0.8)
    ticks = sorted({lo, (lo * hi) ** 0.5 if use_log else (lo + hi) / 2, hi})
    for v in ticks:
        g.text(ml - 5, PY(v) + 3, price_fmt(v), 8, "end", MUT)

    labs = {4: "10K", 5: "100K", 6: "1M", 7: "10M", 8: "100M", 9: "1B", 10: "10B", 11: "100B"}
    for dec in range(int(vlo), int(vhi) + 2):
        yy = VYraw(10 ** dec)
        if vy0 - 0.5 <= yy <= vy1 + 0.5:
            g.line(ml, yy, ml + pw, yy, GRID, 1, None, 0.7)
            if dec == 7:
                continue
            g.text(ml - 5, yy + 3, labs.get(dec, f"1e{dec}"), 7.5, "end", MUT)
    ty = VYraw(THRESH)
    if vy0 + 8 <= ty <= vy1 - 4:
        g.line(ml, ty, ml + pw, ty, ORANGE, 1, "4 3", 0.9)
        g.text(ml - 5, ty + 3, "$10M", 7.5, "end", ORANGE, "700")

    step = pw / n
    bw = max(step * 0.62, 1.2)
    for i, b in enumerate(bars):
        x = ml + step * (i + 0.5)
        col = GREEN if b[4] >= b[1] else RED
        g.rect(x - bw / 2, VY(b[5]), bw, vy1 - VY(b[5]), col, 0, 0.4)
        g.line(x, PY(b[2]), x, PY(b[3]), col, 0.8, None, 0.9)
        top, bot = PY(max(b[1], b[4])), PY(min(b[1], b[4]))
        g.rect(x - bw / 2, top, bw, max(bot - top, 0.8), col, 0, 0.95)

    # x labels
    idx = sorted({0, n // 3, 2 * n // 3, n - 1})
    for i in idx:
        x = ml + step * (i + 0.5)
        g.text(x, vy1 + 13, time.strftime("%y-%m", time.gmtime(bars[i][0] / 1000)), 7.5,
               "start" if i == 0 else "end" if i == n - 1 else "middle", MUT)

    # legend in the gutter
    g.rect(ml + 2, py1 + 5, 7, 7, GREEN, 0, 0.55)
    g.text(ml + 12, py1 + 11.5, "现货成交额" if rec["venue"] == "spot" else "合约成交额", 7.5, "start", MUT)
    g.text(ml + 62, py1 + 11.5, f"（{pick_gran(len(rows_win))[1]}均）", 7.5, "start", MUT)
    g.line(ml + 118, py1 + 8.5, ml + 130, py1 + 8.5, ORANGE, 1.4)
    g.text(ml + 134, py1 + 11.5, "$10M/日 阈值", 7.5, "start", MUT)

    # title
    tag = "合约独有" if rec["venue"] == "futures" else "现货+合约"
    g.text(4, 14, f"{base}/USDT", 13, "start", INK, "700")
    tw = (len(base) + 5) * 7.7
    g.text(4 + tw + 8, 14, tag, 8, "start", BLUE, "700")
    g.text(4, 27, title_note, 8.5, "start", MUT)
    g.text(cw - 4, 14, f"期内 {rec['ret']:+.0f}%", 10, "end", GREEN if rec["ret"] >= 0 else RED, "700")
    g.text(cw - 4, 27, f"日均振幅 {rec['avg_range']:.1f}% · 中位成交额 {money(rec['med_qv'])}", 8.5, "end", MUT)
    if use_log:
        g.text(cw - 4, 38, "价格轴：对数", 7.5, "end", MUT)
    return g.out(f"{base} 走势图")


# ---------------------------------------------------------------- comparison charts
def scatter_chart(feats, sel_syms):
    w, h = 560, 380
    ml, mr, mt, mb = 52, 16, 46, 44
    g = W(w, h)
    pw, ph = w - ml - mr, h - mt - mb
    xs = [math.log10(max(p["med_qv"], 1000)) for p in feats]
    ys = [p["avg_range"] for p in feats]
    x0, x1 = 3.0, 9.5
    y0, y1 = 0, 30
    PX = lambda v: ml + (v - x0) / (x1 - x0) * pw
    PY = lambda v: mt + ph - (v - y0) / (y1 - y0) * ph
    for v in range(4, 10):
        g.line(PX(v), mt, PX(v), mt + ph, GRID, 1, None, 0.8)
        g.text(PX(v), mt + ph + 14, ["", "", "", "", "10K", "100K", "1M", "10M", "100M", "1B"][v] if v <= 9 else "", 8, "middle", MUT)
    for v in range(0, 31, 5):
        g.line(ml, PY(v), ml + pw, PY(v), GRID, 1, None, 0.8)
        g.text(ml - 6, PY(v) + 3, f"{v}%", 8, "end", MUT)
    g.text(ml + pw / 2, h - 6, "中位日成交额（对数轴）", 9, "middle", MUT)
    g.add(f'<g transform="rotate(-90 12 {mt+ph/2})">')
    g.text(12, mt + ph / 2, "日均振幅", 9, "middle", MUT)
    g.add("</g>")
    for p in feats:
        sx = PX(math.log10(max(p["med_qv"], 1000)))
        sy = PY(min(p["avg_range"], y1 - 0.2))
        if p["symbol"] in EX:
            g.circle(sx, sy, 4.5, "#dc2626", 0.95, "#ffffff", 1.2)
        elif p["symbol"] in sel_syms:
            g.circle(sx, sy, 3.4, "#ea8a1f", 0.85)
        else:
            g.circle(sx, sy, 2.2, "#94a3b8", 0.4)
    for p in feats:
        if p["symbol"] in EX:
            g.text(PX(math.log10(max(p["med_qv"], 1000))) + 7, PY(min(p["avg_range"], y1 - .2)) + 3,
                   p["base"], 8, "start", RED, "700")
    g.text(ml, mt - 22, "灰点=全样本（现货+合约独有）　橙点=同款名单　红点=四个示例", 8.5, "start", MUT)
    g.text(ml, mt - 8, "左上：低量高波动（薄流动性）　右上：高量高波动（真正的同款区）", 8.5, "start", MUT)
    return g.out("波动与成交额分布")


def dist_chart(feats):
    A = [p for p in feats if p["venue"] == "spot"]
    B = [p for p in feats if p["venue"] != "spot"]
    w, h = 560, 250
    ml, mr, mt, mb = 46, 14, 40, 40
    g = W(w, h)
    pw, ph = w - ml - mr, h - mt - mb
    qs = [("p50", 0.5), ("p75", 0.75), ("p90", 0.9), ("最大", 1.0)]
    vals = {}
    for name, grp in (("现货币", A), ("合约独有", B)):
        s = sorted(p["avg_range"] for p in grp)
        vals[name] = [s[min(int(len(s) * q), len(s) - 1)] for _, q in qs]
    ymax = 30
    PY = lambda v: mt + ph - v / ymax * ph
    for v in (0, 10, 20, 30):
        g.line(ml, PY(v), ml + pw, PY(v), GRID, 1, None, 0.9)
        g.text(ml - 6, PY(v) + 3, f"{v}%", 8, "end", MUT)
    grp_w = pw / len(qs)
    for gi, (name, col) in enumerate((("现货币", "#94a3b8"), ("合约独有", "#ea8a1f"))):
        for qi, (lab, _) in enumerate(qs):
            x = ml + grp_w * (qi + 0.5) + (gi - 0.5) * grp_w * 0.34
            v = vals[name][qi]
            bw = grp_w * 0.28
            g.rect(x - bw / 2, PY(v), bw, mt + ph - PY(v), col, 2, 0.85)
            g.text(x, PY(v) - 4, f"{v:.1f}", 7.5, "middle", col, "700")
    for qi, (lab, _) in enumerate(qs):
        g.text(ml + grp_w * (qi + 0.5), mt + ph + 15, lab, 8.5, "middle", MUT)
    g.text(ml + pw / 2, h - 6, "日均振幅（以发行后第 60 天为起点）", 9, "middle", MUT)
    g.rect(ml, mt - 14, 8, 8, "#94a3b8", 0, 0.85); g.text(ml + 12, mt - 7, "现货币 n=138", 8, "start", MUT)
    g.rect(ml + 100, mt - 14, 8, 8, "#ea8a1f", 0, 0.85); g.text(ml + 112, mt - 7, "合约独有 n=301", 8, "start", MUT)
    return g.out("波动率分布对比")


def main():
    feats = json.load(open(os.path.join(DATA, "features.json"), encoding="utf-8"))
    sel = json.load(open(os.path.join(DATA, "selection.json"), encoding="utf-8"))
    selected = sel["selected"]
    for p_ in sel["selected"]:
        if p_["symbol"] in feats:
            feats[p_["symbol"]]["score"] = p_["score"]
    sel_syms = {p["symbol"] for p in selected}
    load_rows.uni = json.load(open(os.path.join(DATA, "universe.json"), encoding="utf-8"))["symbols"]

    svg = {}
    # examples: full history + post-60 window
    for s in EX:
        rec = feats[s]
        rows = load_rows(rec)
        cut = rows[0][0] + 60 * DAY
        full = chart(rec, rows, None,
                     f"上线 {d2s(rows[0][0])} · 全周期 {len(rows)} 天（虚竖线=发行后第60天）")
        # redraw full with a day-60 marker
        full = chart_full(rec, rows, cut)
        svg[s + "_full"] = full
        post = [r for r in rows if r[0] >= cut]
        svg[s + "_post"] = chart(rec, post, None,
                                 f"发行后第60天起 · {d2s(post[0][0])}→{d2s(post[-1][0])} · {len(post)} 天")

    # selected coins: post-60 window only
    for p in selected:
        rec = feats[p["symbol"]]
        rows = load_rows(rec)
        cut = rows[0][0] + 60 * DAY
        post = [r for r in rows if r[0] >= cut]
        svg[p["symbol"]] = chart(rec, post, None,
                                 f"发行后第60天起 · {d2s(post[0][0])}→{d2s(post[-1][0])} · {len(post)} 天")

    svg['__dist'] = dist_chart(list(feats.values()))
    svg['__scatter'] = scatter_chart(list(feats.values()), sel_syms)

    files = {}
    for k, v in svg.items():
        if k.startswith('__'):
            continue
        if k.endswith("_full"):
            name = k.replace("USDT", "").replace("_full", "-全周期")
        elif k.endswith("_post"):
            name = k.replace("USDT", "").replace("_post", "-后60天")
        else:
            name = k.replace("USDT", "") + "-后60天"
        open(os.path.join(CH, name + ".svg"), "w", encoding="utf-8").write(
            v.replace('width="100%"', 'width="1120" height="680"'))
        files[k] = name

    json.dump({"files": files}, open(os.path.join(DATA, "charts.json"), "w", encoding="utf-8"))

    open(os.path.join(CH, "分布-波动对比.svg"), "w", encoding="utf-8").write(
        svg['__dist'].replace('width="100%"', 'width="1120" height="500"'))
    open(os.path.join(CH, "分布-散点.svg"), "w", encoding="utf-8").write(
        svg['__scatter'].replace('width="100%"', 'width="1120" height="760"'))
    print("charts:", len(files) + 2)

    build_html(feats, selected, svg, sel)


def chart_full(rec, rows, cut):
    """full history chart with a vertical marker at day 60"""
    base = rec["base"]
    bars = aggregate(rows, pick_gran(len(rows))[0])
    n = len(bars)
    w, h = 560, 340
    ml, mr = 46, 10
    py0, py1 = 46, h - 108
    vy0, vy1 = h - 96, h - 34
    pw = w - ml - mr
    g = W(w, h)
    hi = max(b[2] for b in bars); lo = min(b[3] for b in bars)
    use_log = hi / lo > 2.5 if lo > 0 else False
    if use_log:
        plo, phi = math.log10(lo) - 0.05, math.log10(hi) + 0.05
        tf = lambda v: (math.log10(max(v, 1e-12)) - plo) / (phi - plo)
    else:
        pad = (hi - lo) * 0.08 or hi * 0.05
        plo, phi = lo - pad, hi + pad
        tf = lambda v: (v - plo) / (phi - plo)
    PY = lambda v: min(max(py1 - tf(v) * (py1 - py0), py0 + 0.5), py1 - 0.5)
    q = [b[5] for b in bars]
    vmax = max(max(q), THRESH * 1.4)
    vmin = max(min([x for x in q if x > 0] or [1e5]), 1e4)
    vlo, vhi = math.log10(vmin * 0.55), math.log10(vmax * 1.3)
    VYraw = lambda v: vy1 - ((math.log10(max(v, 1e-9)) - vlo) / (vhi - vlo)) * (vy1 - vy0)
    VY = lambda v: min(max(VYraw(v), vy0 + 0.4), vy1)
    g.rect(ml, py0, pw, py1 - py0, "#fcfdfe")
    g.rect(ml, vy0, pw, vy1 - vy0, "#fcfdfe")
    for f in (0.5,):
        g.line(ml, py1 - f * (py1 - py0), ml + pw, py1 - f * (py1 - py0), GRID, 1, None, 0.8)
    mid = (lo * hi) ** 0.5 if use_log else (lo + hi) / 2
    for v in sorted({lo, mid, hi}):
        g.text(ml - 5, PY(v) + 3, price_fmt(v), 8, "end", MUT)
    labs = {4: "10K", 5: "100K", 6: "1M", 7: "10M", 8: "100M", 9: "1B", 10: "10B"}
    for dec in range(int(vlo), int(vhi) + 2):
        yy = VYraw(10 ** dec)
        if vy0 - 0.5 <= yy <= vy1 + 0.5:
            g.line(ml, yy, ml + pw, yy, GRID, 1, None, 0.7)
            if dec == 7: continue
            g.text(ml - 5, yy + 3, labs.get(dec, f"1e{dec}"), 7.5, "end", MUT)
    ty = VYraw(THRESH)
    if vy0 + 8 <= ty <= vy1 - 4:
        g.line(ml, ty, ml + pw, ty, ORANGE, 1, "4 3", 0.9)
        g.text(ml - 5, ty + 3, "$10M", 7.5, "end", ORANGE, "700")
    step = pw / n
    bw = max(step * 0.62, 1.0)
    for i, b in enumerate(bars):
        x = ml + step * (i + 0.5)
        col = GREEN if b[4] >= b[1] else RED
        g.rect(x - bw / 2, VY(b[5]), bw, vy1 - VY(b[5]), col, 0, 0.4)
        g.line(x, PY(b[2]), x, PY(b[3]), col, 0.8, None, 0.9)
        top, bot = PY(max(b[1], b[4])), PY(min(b[1], b[4]))
        g.rect(x - bw / 2, top, bw, max(bot - top, 0.8), col, 0, 0.95)
    # day-60 marker
    idx = sum(1 for b in bars if b[0] < cut)
    xm = ml + step * idx
    g.line(xm, py0, xm, vy1, BLUE, 1.2, "4 3", 0.8)
    g.text(xm + 3, py0 + 10, "发行后第60天", 7.5, "start", BLUE, "700")
    for i in sorted({0, n // 3, 2 * n // 3, n - 1}):
        x = ml + step * (i + 0.5)
        g.text(x, vy1 + 13, time.strftime("%y-%m", time.gmtime(bars[i][0] / 1000)), 7.5,
               "start" if i == 0 else "end" if i == n - 1 else "middle", MUT)
    g.rect(ml + 2, py1 + 5, 7, 7, GREEN, 0, 0.55)
    g.text(ml + 12, py1 + 11.5, "成交额", 7.5, "start", MUT)
    g.text(4, 14, f"{base}/USDT", 13, "start", INK, "700")
    g.text(4, 27, f"上市至今 {len(rows)} 天 · 区间 {d2s(rows[0][0])}→{d2s(rows[-1][0])}", 8.5, "start", MUT)
    g.text(w - 4, 14, f"{rows[-1][4]:.6g}", 10, "end", INK, "700")
    g.text(w - 4, 27, f"较最高点 {min(1.0, rows[-1][4]/max(r[2] for r in rows))*100-100:+.0f}%", 8.5, "end", MUT)
    return g.out(f"{base} 全周期")


def build_html(feats, selected, svg, sel):
    from html_parts import page
    html = page(feats, selected, svg, sel)
    p = os.path.join(BASE, "高波动合约币同款分析.html")
    open(p, "w", encoding="utf-8").write(html)
    print("written", p, len(html))


if __name__ == "__main__":
    main()
