# -*- coding: utf-8 -*-
"""
策略选股引擎（第三功能模块）
============================
在保留原有两套「全条件严格策略」的基础上，新增可直接运行、命中率更高的
形态选股体系（日线 + 周线），并打通全市场扫描、进度反馈、可中断与增量结果。

一、形态标签（命中即打标签，按权重累加为形态分）
  日线：D均线多头 / D均线金叉 / D放量突破 / D回调企稳 / D强势动量 /
        D量价齐升 / D均线粘合 / DMACD金叉 / D缩量回踩MA20
  周线：W周线多头 / W周线金叉 / W周线放量 / W周线突破 / W周线回调
  市值区间、板块、流动性（成交额/换手）在扫描前做硬过滤。

二、严格策略（原规则完整保留）
  trend     趋势信号（31 项条件 + 8 项持仓风控）
  reversal  回调反转·最冷清的一天（22 项条件）
  all       两套严格策略任一命中即可

三、数据与性能
  行情：东财全市场清单（带换手/市值/行业），失败自动降级腾讯批量行情；
  日线：新浪 → 腾讯 → 东财（fetch_daily_fast，短超时 + 日线磁盘缓存）；
  扫描：候选股并发分析（默认 10 线程），结果按批回调（边算边出），
        支持 stop_event 中断；单只失败只跳过该股，不影响整体。

注意：结果为量化模型整理，不构成投资建议。
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

import numpy as np
import pandas as pd

from data_sources import fetch_all_quotes, fetch_daily_fast, is_trading_time

# ============================================================
#  形态定义与权重
# ============================================================
PATTERN_WEIGHTS = {
    "D均线多头": 12, "D均线金叉": 8, "D放量突破": 12, "D回调企稳": 10,
    "D强势动量": 8, "D量价齐升": 8, "D均线粘合": 8, "DMACD金叉": 8,
    "D缩量回踩MA20": 6,
    "W周线多头": 12, "W周线金叉": 10, "W周线放量": 6,
    "W周线突破": 10, "W周线回调": 6,
}

STRATEGIES = {}          # 在文件末尾填充（严格策略依赖下方规则函数）
STRATEGY_NAMES = {
    "pattern": "多形态组合（日线+周线）",
    "ma_bull": "日线均线多头",
    "breakout": "放量突破",
    "pullback": "回调企稳",
    "weekly_golden": "周线金叉",
    "weekly_bull": "周线多头",
    "weekly_breakout": "周线突破",
    "trend": "趋势信号（31项严格）",
    "reversal": "回调反转（22项严格）",
    "all": "严格策略任一命中",
}


# ============================================================
#  指标预计算
# ============================================================
def _atr(df: pd.DataFrame, n: int = 10) -> np.ndarray:
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c),
                                      np.abs(l - prev_c)))
    s = pd.Series(tr).rolling(n).mean().values
    return s


def _ma_series(df: pd.DataFrame, n: int) -> np.ndarray:
    return df["close"].rolling(n).mean().values


def precompute(df: pd.DataFrame, price: float, total_mv: float) -> dict:
    """一次算好全部指标序列与窗口切片，供各条件判定使用。"""
    close = df["close"].values
    open_ = df["open"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["volume"].values
    atr = _atr(df, 10)

    ret = np.zeros(len(close))
    ret[1:] = close[1:] / close[:-1] - 1.0
    # 量比：当日量 / 前5日均量（不含当日）
    vol_ratio = np.zeros(len(close))
    for i in range(6, len(close)):
        base = vol[i - 5:i - 1].mean()
        vol_ratio[i] = vol[i] / base if base > 0 else 1.0

    ma = {n: _ma_series(df, n) for n in (5, 10, 20, 30, 60, 120, 250)}
    entity = np.abs(close - open_)
    up_shadow = high - np.maximum(close, open_)
    dn_shadow = np.minimum(close, open_) - low

    return {
        "close": close, "open": open_, "high": high, "low": low,
        "vol": vol, "atr": atr, "ret": ret, "vol_ratio": vol_ratio,
        "ma": ma, "entity": entity,
        "up_shadow": up_shadow, "dn_shadow": dn_shadow,
        "price": price, "total_mv": total_mv,
    }


# ============================================================
#  形态特征：日线 + 周线
# ============================================================
def _macd(close: np.ndarray, fast: int = 12, slow: int = 26,
          signal: int = 9):
    """MACD（EMA 实现），返回 (dif, dea, hist)。"""
    s = pd.Series(close)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    dif = (ema_fast - ema_slow).values
    dea = pd.Series(dif).ewm(span=signal, adjust=False).mean().values
    return dif, dea, dif - dea


def _to_weekly(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """日线 → 周线（按自然周聚合，周五收盘）。"""
    try:
        w = df.resample("W-FRI").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum", "amount": "sum",
        }).dropna()
        return w if len(w) >= 20 else None
    except Exception:
        return None


def _weekly_features(df: pd.DataFrame) -> Optional[dict]:
    w = _to_weekly(df)
    if w is None:
        return None
    wc, wo, wh, wv = (w["close"].values, w["open"].values,
                      w["high"].values, w["volume"].values)
    wma = {n: w["close"].rolling(n).mean().values
           for n in (5, 10, 20, 30)}
    dif, dea, _ = _macd(wc)
    n = len(wc)
    gold_recent = False
    for i in range(max(1, n - 3), n):
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            gold_recent = True
    ma_gold = False
    if n >= 12:
        for i in range(n - 3, n):
            if wma[5][i] > wma[10][i] and wma[5][i - 1] <= wma[10][i - 1]:
                ma_gold = True
    bull = (n >= 21 and wma[5][-1] > wma[10][-1] > wma[20][-1]
            and wc[-1] > wma[20][-1] and wma[5][-1] > wma[5][-2])
    vol_base = float(np.mean(wv[-5:-1])) if len(wv) >= 6 else 0.0
    vol_burst = vol_base > 0 and wv[-1] > 1.4 * vol_base
    prior_high = float(np.max(wh[-14:-1])) if n >= 15 else float("nan")
    breakout = n >= 15 and wc[-1] > prior_high
    ret4 = wc[-1] / wc[-5] - 1 if n >= 5 else 0.0
    dd = (float(np.max(wh[-12:])) - wc[-1]) / float(np.max(wh[-12:])) \
        if n >= 12 else 0.0
    pullback = (ret4 > 0.03 and wc[-1] > wma[10][-1]
                and 0.02 <= dd <= 0.15 and wc[-1] > wo[-1])
    return {"n": n, "close": wc, "ma": wma, "dif": dif, "dea": dea,
            "gold": gold_recent or ma_gold, "bull": bull,
            "vol_burst": vol_burst, "breakout": breakout,
            "pullback": pullback, "ret4": ret4, "dd": dd,
            "vol_base": vol_base, "last_vol": float(wv[-1]),
            "prior_high": prior_high}


def daily_features(df: pd.DataFrame) -> Optional[dict]:
    """计算日线形态所需的全部特征（不含市值/换手等快照字段）。"""
    if df is None or len(df) < 60:
        return None
    c, o, h, l, v = (df["close"].values, df["open"].values,
                     df["high"].values, df["low"].values,
                     df["volume"].values)
    n = len(c)
    ma = {p: df["close"].rolling(p).mean().values
          for p in (5, 10, 20, 30, 60, 120)}
    dif, dea, _ = _macd(c)
    ret20 = c[-1] / c[-21] - 1 if n >= 21 else 0.0
    ret5 = c[-1] / c[-6] - 1 if n >= 6 else 0.0
    vol_base = float(np.mean(v[-6:-1])) if n >= 6 else 0.0
    vol_ratio = float(v[-1] / vol_base) if vol_base > 0 else 1.0
    atr = _atr(df, 10)
    atr_pct = float(atr[-1] / c[-1]) if c[-1] > 0 else 0.0
    high20 = float(np.max(h[-21:-1])) if n >= 21 else float(h[-1])
    close_high20 = float(np.max(c[-21:-1])) if n >= 21 else float(c[-1])
    dd20 = (float(np.max(h[-20:])) - c[-1]) / float(np.max(h[-20:])) \
        if n >= 20 and np.max(h[-20:]) > 0 else 0.0

    gold = False
    for i in range(max(1, n - 3), n):
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            gold = True
    ma_gold = False
    for i in range(max(1, n - 3), n):
        if ma[5][i] > ma[10][i] and ma[5][i - 1] <= ma[10][i - 1]:
            ma_gold = True
        if ma[5][i] > ma[20][i] and ma[5][i - 1] <= ma[20][i - 1]:
            ma_gold = True

    ma_bull = (ma[5][-1] > ma[10][-1] > ma[20][-1]
              and c[-1] > ma[20][-1]
              and (n < 11 or ma[20][-1] >= ma[20][-11]))
    spread = 0.0
    vals = [ma[p][-1] for p in (5, 10, 20, 60)]
    if all(np.isfinite(x) for x in vals) and min(vals) > 0:
        spread = (max(vals) - min(vals)) / min(vals)
    sticky = spread > 0 and spread < 0.06 and c[-1] > max(vals)

    breakout = (c[-1] > close_high20 and vol_ratio >= 1.4
                and c[-1] > o[-1] and (c[-1] / c[-2] - 1) > 0.005)
    pullback = (ret20 > 0.03 and 0.02 <= dd20 <= 0.15
                and c[-1] > ma[20][-1] and c[-1] > o[-1]
                and vol_ratio < 1.0)
    momentum = (ret20 >= 0.08 and ret5 > 0
                and (high20 - c[-1]) / high20 <= 0.03)
    vol_price = (vol_ratio >= 1.3 and c[-1] > o[-1]
                 and (c[-1] / c[-2] - 1) > 0.01)
    pull_ma20 = (abs(c[-1] / ma[20][-1] - 1) <= 0.02
                 and l[-1] >= ma[20][-1] * 0.985
                 and ma[20][-1] > ma[60][-1]
                 and vol_ratio < 0.85) if np.isfinite(ma[60][-1]) else False

    return {
        "n": n, "close": float(c[-1]), "open": float(o[-1]),
        "ret1": float(c[-1] / c[-2] - 1) if len(c) >= 2 and c[-2] > 0 else 0.0,
        "ret5": ret5, "ret20": ret20, "vol_ratio": vol_ratio,
        "atr_pct": atr_pct, "high20": high20, "close_high20": close_high20,
        "dd20": dd20, "ma": ma, "dif": dif, "dea": dea,
        "gold": gold or ma_gold, "x_ma5_ma10": ma_gold,
        "ma_bull": ma_bull, "spread": spread, "sticky": sticky,
        "breakout": breakout, "pullback": pullback,
        "momentum": momentum, "vol_price": vol_price,
        "pull_ma20": pull_ma20,
        "last_vol": float(v[-1]), "vol_base": vol_base,
    }


def pattern_tags(df: pd.DataFrame, feats: Optional[dict] = None,
                 wfeats: Optional[dict] = None,
                 with_weekly: bool = True) -> tuple:
    """
    返回 (tags:list[str], details:list[str], features:dict|None,
          weekly_features:dict|None, tags_by_group:dict)。
    """
    f = feats or daily_features(df)
    if f is None:
        return [], [], None, None, {}
    wf = wfeats if wfeats is not None else (
        _weekly_features(df) if with_weekly else None)
    tags, details = [], []
    groups = {"日线形态": [], "周线形态": []}

    def add(tag, ok, detail, group="日线形态"):
        if ok:
            tags.append(tag)
            details.append(f"{tag}（{detail}）")
            groups[group].append(tag)

    add("D均线多头", f["ma_bull"],
        f"MA5 {f['ma'][5][-1]:.2f} > MA10 {f['ma'][10][-1]:.2f} > "
        f"MA20 {f['ma'][20][-1]:.2f}")
    add("D均线金叉", f["x_ma5_ma10"], "MA5 近期上穿 MA10/MA20")
    add("D放量突破", f["breakout"],
        f"突破20日收盘高 {f['close_high20']:.2f}，量比 {f['vol_ratio']:.2f}")
    add("D回调企稳", f["pullback"],
        f"20日回撤 {f['dd20']:.1%}，量比 {f['vol_ratio']:.2f}，收阳")
    add("D强势动量", f["momentum"], f"20日涨幅 {f['ret20']:+.1%}")
    add("D量价齐升", f["vol_price"],
        f"量比 {f['vol_ratio']:.2f}，当日涨幅 {f['ret1']:+.1%}")
    add("D均线粘合", f["sticky"], f"均线离散度 {f['spread']:.1%} 后向上突破")
    add("DMACD金叉", f["gold"], "DIF 近期上穿 DEA")
    add("D缩量回踩MA20", f["pull_ma20"],
        f"贴近MA20（{f['ma'][20][-1]:.2f}），缩量 {f['vol_ratio']:.2f}")

    if wf:
        add("W周线多头", wf["bull"],
            f"周 MA5 {wf['ma'][5][-1]:.2f} > MA10 {wf['ma'][10][-1]:.2f} > "
            f"MA20 {wf['ma'][20][-1]:.2f}", "周线形态")
        add("W周线金叉", wf["gold"], "周线 MACD/均线近期金叉", "周线形态")
        add("W周线放量", wf["vol_burst"],
            f"周量 {wf['last_vol']:.0f} vs 4周均 {wf['vol_base']:.0f}", "周线形态")
        add("W周线突破", wf["breakout"],
            f"周收盘突破13周高 {wf['prior_high']:.2f}", "周线形态")
        add("W周线回调", wf["pullback"],
            f"4周涨幅 {wf['ret4']:+.1%}，回撤 {wf['dd']:.1%}", "周线形态")

    return tags, details, f, wf, groups


def _pattern_score(tags: List[str]) -> int:
    return int(sum(PATTERN_WEIGHTS.get(t, 0) for t in tags))


# ============================================================
#  策略一：趋势信号（31 项选股条件）
# ============================================================
def _trend_rules(ctx: dict, code: str) -> List[dict]:
    c, o, h, l, v = (ctx["close"], ctx["open"], ctx["high"],
                     ctx["low"], ctx["vol"])
    atr, r, vr = ctx["atr"], ctx["ret"], ctx["vol_ratio"]
    ma, e, us, ds = (ctx["ma"], ctx["entity"],
                     ctx["up_shadow"], ctx["dn_shadow"])
    price, mv = ctx["price"], ctx["total_mv"]
    n = len(c)
    is_kcb = code.startswith("68")

    def rng(*idx):  # 窗口取值（负索引安全）
        return [c[i] for i in idx]

    rules = []

    def add(no, name, ok, cur="", req=""):
        rules.append({"no": no, "name": name, "ok": bool(ok),
                      "cur": cur, "req": req})

    # --- 前置过滤 ---
    add("①", "总市值 50亿~10000亿", 50 <= mv <= 10000,
        f"{mv:.0f}亿", "50 ~ 10000 亿")
    add("②", "股价 5~300元（科创≤200）",
        5 <= price <= (200 if is_kcb else 300),
        f"{price:.2f}元", "5~300（科创≤200）")
    add("③", "22日涨幅 ≤ 50%",
        (c[-1] / c[-23] - 1) <= 0.50,
        f"{c[-1]/c[-23]-1:+.1%}", "≤ +50%")

    # --- 波动率 ---
    atr_r = atr[-1] / c[-1]
    add("④", "ATR10/收盘价 3.1%~5.1%",
        0.031 <= atr_r <= 0.051,
        f"{atr_r:.2%}", "3.1% ~ 5.1%")

    # --- 长期趋势 ---
    add("⑤", "MA120 ≥ 1.023×MA250",
        ma[120][-1] >= 1.023 * ma[250][-1],
        f"{ma[120][-1]:.2f} vs {1.023*ma[250][-1]:.2f}",
        "MA120 ≥ 1.023×MA250")

    # --- 节奏控制 ---
    consec = 0
    for i in range(n - 1, 0, -1):
        if c[i] > c[i - 1]:
            consec += 1
        else:
            break
    add("⑥", "连涨天数 ≤ 4天", consec <= 4, f"{consec}天", "≤ 4天")
    up9 = int(sum(1 for i in range(n - 9, n) if r[i] > 0))
    add("⑦", "近9天上涨天数 ≤ 6天", up9 <= 6, f"{up9}天", "≤ 6天")

    # --- MA5 加速 ---
    add("⑧", "MA5[-1] > 1.004×MA5[-2]",
        ma[5][-1] > 1.004 * ma[5][-2],
        f"{ma[5][-1]:.3f} vs {1.004*ma[5][-2]:.3f}", "加速 0.4%")
    add("⑨", "MA5[-2] > 1.002×MA5[-3]",
        ma[5][-2] > 1.002 * ma[5][-3],
        f"{ma[5][-2]:.3f} vs {1.002*ma[5][-3]:.3f}", "加速 0.2%")
    add("⑩", "MA5[-3] > 1.001×MA5[-4]",
        ma[5][-3] > 1.001 * ma[5][-4],
        f"{ma[5][-3]:.3f} vs {1.001*ma[5][-4]:.3f}", "抬头 0.1%")

    # --- 均线多头排列 ---
    add("⑪", "D0收盘 > MA20 > MA30 > MA60",
        c[-1] > ma[20][-1] > ma[30][-1] > ma[60][-1],
        f"{c[-1]:.2f}>{ma[20][-1]:.2f}>{ma[30][-1]:.2f}>{ma[60][-1]:.2f}",
        "收盘>MA20>MA30>MA60")
    add("⑫", "MA5 ≥ MA10",
        ma[5][-1] >= ma[10][-1],
        f"{ma[5][-1]:.2f} vs {ma[10][-1]:.2f}", "MA5 ≥ MA10")

    # --- 收盘价与 MA5 ---
    add("⑬", "D0收盘 > 1.01×MA5[-1]",
        c[-1] > 1.01 * ma[5][-1],
        f"{c[-1]:.2f} vs {1.01*ma[5][-1]:.2f}", "> 5日线 1%")
    add("⑭", "D-1收盘 > 1.005×MA5[-2]",
        c[-2] > 1.005 * ma[5][-2],
        f"{c[-2]:.2f} vs {1.005*ma[5][-2]:.2f}", "> 5日线 0.5%")
    add("⑮", "D-2收盘 > MA5[-3]",
        c[-3] > ma[5][-3],
        f"{c[-3]:.2f} vs {ma[5][-3]:.2f}", "> 5日线")

    # --- 最低价与 MA5 ---
    add("⑯", "D0最低 > 0.99×MA5[-1]",
        l[-1] > 0.99 * ma[5][-1],
        f"{l[-1]:.2f} vs {0.99*ma[5][-1]:.2f}", "守住 5日线 99%")
    add("⑰", "D-1最低 > 0.98×MA5[-2]",
        l[-2] > 0.98 * ma[5][-2],
        f"{l[-2]:.2f} vs {0.98*ma[5][-2]:.2f}", "守住 5日线 98%")

    # --- K线形态（一）基础 ---
    yang4 = sum(1 for i in range(n - 4, n) if c[i] > o[i])
    add("⑱", "近4天≥3阳 + D0阳线",
        yang4 >= 3 and c[-1] > o[-1],
        f"{yang4}阳/4天", "≥3阳且今日阳")
    up4 = [c[i] / c[i - 1] - 1 for i in range(n - 4, n)]
    add("⑲", "连续4天收盘涨 ≥0.5%",
        all(x >= 0.005 for x in up4),
        "、".join(f"{x:+.2%}" for x in up4), "每天 ≥ +0.5%")
    add("⑳", "D0最高 > 前3天最高",
        h[-1] > max(h[-4:-1]),
        f"{h[-1]:.2f} vs {max(h[-4:-1]):.2f}", "创近4日新高")
    add("㉑", "D0收盘 > 前3天开盘价",
        c[-1] > max(o[-4:-1]),
        f"{c[-1]:.2f} vs {max(o[-4:-1]):.2f}", "解套套牢盘")

    # --- K线形态（二）涨幅节奏 ---
    ratio_ok = True
    ratio_txt = []
    for i in range(n - 3, n):
        denom = r[i - 1]
        ratio_ok &= (denom > 0 and r[i] / denom >= 0.5)
        ratio_txt.append(f"{r[i]/denom:.2f}" if denom > 0 else "∞")
    add("㉒", "相邻涨幅比 ≥ 0.5（全部）",
        ratio_ok, "、".join(ratio_txt), "3 个比值全部 ≥ 0.5")
    shrink = (r[-1] < 0.995 * r[-2] and r[-2] < 0.995 * r[-3]
              and r[-3] < 0.995 * r[-4])
    add("㉓", "不允许连续4天涨幅递减",
        not shrink,
        f"{r[-1]:+.2%},{r[-2]:+.2%},{r[-3]:+.2%},{r[-4]:+.2%}",
        "连续递减才排除")

    # --- K线形态（三）排除弱势 ---
    add("㉔", "每日涨幅 < 6.5%",
        all(x < 0.065 for x in r[-4:]),
        f"近4日最高 {max(r[-4:]):+.2%}", "全部 < 6.5%")
    atr_ok = True
    for i in range(n - 4, n):
        if c[i - 1] > 0 and r[i] >= 2 * atr[i] / c[i - 1]:
            atr_ok = False
    add("㉕", "每日涨幅 < 2倍当日ATR",
        atr_ok, f"ATR比 {atr[-1]/c[-1]:.2%}", "涨幅 < 2×ATR")
    add("㉖", "D0涨幅不能是近4天最低",
        r[-1] > min(r[-4:-1]),
        f"D0 {r[-1]:+.2%} vs 近4天 {min(r[-4:-1]):+.2%}",
        "信号日非最弱")
    e_shrink = (e[-1] < e[-2] and e[-2] < e[-3])
    add("㉗", "阳线实体不连续缩小",
        not e_shrink,
        f"{e[-1]:.2f}<{e[-2]:.2f}<{e[-3]:.2f}" if e_shrink else
        f"{e[-1]:.2f},{e[-2]:.2f},{e[-3]:.2f}",
        "连续3天缩短才排除")
    up_sh, dn_sh = us[-1], ds[-1]
    long_upper = (up_sh > 1.5 * e[-1] or up_sh > 0.8 * (c[-1] - c[-2])) \
        and up_sh > 1.5 * dn_sh
    add("㉘", "长上影线排除",
        not long_upper,
        f"上影{up_sh:.2f} 下影{dn_sh:.2f} 实体{e[-1]:.2f}",
        "(上影>1.5实体 或 >0.8涨幅)且>1.5下影 才排除")

    # --- 量能 ---
    vr4 = vr[-4:]
    add("㉙", "连续4天量比 0.8~1.8",
        all(0.8 <= x <= 1.8 for x in vr4),
        "、".join(f"{x:.2f}" for x in vr4), "每日 0.8~1.8")
    shrink3 = (v[-1] <= 0.95 * v[-2] and v[-2] <= 0.95 * v[-3]
               and v[-3] <= 0.95 * v[-4])
    add("㉚", "不允许连续3天缩量>5%",
        not shrink3,
        f"{v[-1]:.0f}<{v[-2]:.0f}<{v[-3]:.0f}" if shrink3 else
        f"{v[-1]:.0f},{v[-2]:.0f},{v[-3]:.0f}",
        "连缩3天才排除")
    add("㉛", "D0成交量不能是近4天最低",
        v[-1] > min(v[-4:-1]),
        f"D0 {v[-1]:.0f} vs 前3天最低 {min(v[-4:-1]):.0f}",
        "信号日量非最低")

    return rules


# 策略一风控（持仓阶段使用，随信号一并展示）
TREND_RISK = [
    ("㉟", "初始止损", "买入价 − 1.0×买入日ATR10，控制单笔最大亏损"),
    ("㊱", "保本线上移", "收盘涨幅 ≥ 1.0×买入日ATR10 时，止损提至买入价×1.006"),
    ("㊲", "跟踪止损", "历史最高价 − 2.0×当日ATR10，趋势结束再离场"),
    ("㊳", "主动离场", "放量长阴 + 收盘<买入价，利润脆弱期主动规避"),
    ("㊴", "仓位减半", "全市场下跌家数 > 4000（普跌避险）"),
    ("㊵", "仓位减半", "上证 MA120 < MA250（中长期空头）"),
    ("㊶", "仓位减半", "同一策略连续亏损 ≥ 3笔（不应期收缩）"),
    ("㊷", "仓位减半", "上证单日跌幅 > 1.5%（恐慌日避险）"),
]


# ============================================================
#  策略二：回调反转 · 最冷清的一天（22 项条件）
# ============================================================
def _reversal_rules(ctx: dict, code: str) -> List[dict]:
    c, o, h, l, v = (ctx["close"], ctx["open"], ctx["high"],
                     ctx["low"], ctx["vol"])
    atr = ctx["atr"]
    ma, e = ctx["ma"], ctx["entity"]
    price, mv = ctx["price"], ctx["total_mv"]
    n = len(c)
    is_kcb = code.startswith("68")

    def add(rules, no, name, ok, cur="", req=""):
        rules.append({"no": no, "name": name, "ok": bool(ok),
                      "cur": cur, "req": req})

    rules = []
    # 前置过滤
    add(rules, "①", "总市值 50亿~100000亿", 50 <= mv <= 100000,
        f"{mv:.0f}亿", "50 ~ 100000 亿")
    add(rules, "②", "股价 5~300元（科创≤200）",
        5 <= price <= (200 if is_kcb else 300),
        f"{price:.2f}元", "5~300（科创≤200）")
    # 波动率
    atr_r = atr[-1] / c[-1]
    add(rules, "③", "ATR10/收盘价 2.5%~7.0%",
        0.025 <= atr_r <= 0.070, f"{atr_r:.2%}", "2.5% ~ 7.0%")
    # 趋势确认
    add(rules, "④", "MA5 ≥ MA30", ma[5][-1] >= ma[30][-1],
        f"{ma[5][-1]:.2f} vs {ma[30][-1]:.2f}", "MA5 ≥ MA30")
    add(rules, "⑤", "MA10 ≥ MA30", ma[10][-1] >= ma[30][-1],
        f"{ma[10][-1]:.2f} vs {ma[30][-1]:.2f}", "MA10 ≥ MA30")
    add(rules, "⑥", "MA20 ≥ 0.99×MA30",
        ma[20][-1] >= 0.99 * ma[30][-1],
        f"{ma[20][-1]:.2f} vs {0.99*ma[30][-1]:.2f}", "允许略低于MA30")
    add(rules, "⑦", "MA30 ≥ 0.97×MA60",
        ma[30][-1] >= 0.97 * ma[60][-1],
        f"{ma[30][-1]:.2f} vs {0.97*ma[60][-1]:.2f}", "允许初期放宽")
    slope20 = (ma[20][-1] - ma[20][-10]) / ma[20][-10] / 9
    slope30 = (ma[30][-1] - ma[30][-10]) / ma[30][-10] / 9
    add(rules, "⑧", "MA20斜率 > 0.001（近10天）",
        slope20 > 0.001, f"{slope20:.5f}", "日均 > 0.001")
    add(rules, "⑨", "MA30斜率 > 0.0005（近10天）",
        slope30 > 0.0005, f"{slope30:.5f}", "日均 > 0.0005")
    add(rules, "⑩", "D0收盘 > 1.005×MA30",
        c[-1] > 1.005 * ma[30][-1],
        f"{c[-1]:.2f} vs {1.005*ma[30][-1]:.2f}", "> MA30 0.5%")
    add(rules, "⑪", "MA120 ≥ 1.023×MA250",
        ma[120][-1] >= 1.023 * ma[250][-1],
        f"{ma[120][-1]:.2f} vs {1.023*ma[250][-1]:.2f}",
        "长期趋势向上")

    # --- 近12天倍量大阳线查找 ---
    big_idx = None
    for i in range(n - 13, n - 2):      # 大阳线在 D-2 之前（留出回调+反转）
        if i < 10:
            continue
        cond_vol1 = v[i] > v[i - 10:i - 1].mean() * 1.5
        cond_vol2 = v[i] > 2.0 * v[i - 1]
        cond_up = (c[i] / c[i - 1] - 1) > 0.05
        cond_body = e[i] >= (h[i] - l[i]) * 0.25
        if cond_vol1 and cond_vol2 and cond_up and cond_body:
            big_idx = i
            break
    if big_idx is not None:
        # 回调期：大阳线次日 ~ D-1
        seg = slice(big_idx + 1, n - 1)
        if seg.start < seg.stop:
            low_min = float(l[seg].min())
            back_amp = (c[big_idx] - low_min) / atr[big_idx]
            back_dp = (c[big_idx] - low_min) / c[big_idx]
        else:
            low_min, back_amp, back_dp = None, None, None
    else:
        low_min, back_amp, back_dp = None, None, None

    add(rules, "⑫", "近12天存在倍量大阳线（量>前10日均1.5倍）",
        big_idx is not None,
        f"量比前10日均 {1.5:.1f}倍" if big_idx is not None else "未找到",
        "近12天内找到")
    add(rules, "⑬", "大阳线量 > 前日量×2.0",
        big_idx is not None and v[big_idx] > 2.0 * v[big_idx - 1],
        f"{v[big_idx]:.0f} vs 前日 {v[big_idx-1]:.0f}" if big_idx else "—",
        "量能爆发")
    add(rules, "⑭", "大阳线涨幅 > 5%",
        big_idx is not None and (c[big_idx] / c[big_idx - 1] - 1) > 0.05,
        f"{c[big_idx]/c[big_idx-1]-1:+.2%}" if big_idx else "—",
        "> +5%")
    add(rules, "⑮", "大阳线实体 ≥ 振幅×25%",
        big_idx is not None
        and e[big_idx] >= (h[big_idx] - l[big_idx]) * 0.25,
        f"实体/振幅 {e[big_idx]/(h[big_idx]-l[big_idx]):.0%}"
        if big_idx and h[big_idx] > l[big_idx] else "—",
        "非长上影假突破")

    # 回调确认
    add(rules, "⑯", "回调幅度 1.3~3.3倍ATR",
        back_amp is not None and 1.3 <= back_amp <= 3.3,
        f"{back_amp:.2f}倍ATR" if back_amp is not None else "—",
        "回调深度合理")
    add(rules, "⑰", "回调深度 ≥ 2%",
        back_dp is not None and back_dp >= 0.02,
        f"{back_dp:.2%}" if back_dp is not None else "—",
        "确有回调动作")

    # 突破确认
    if big_idx is not None:
        after_high = float(c[big_idx:n].max())
        before_high = float(c[max(0, big_idx - 40):big_idx].max())
        broke = after_high > before_high
    else:
        after_high, before_high, broke = None, None, False
    add(rules, "⑱", "大阳线后最高收盘 > 前40天最高",
        broke,
        f"{after_high:.2f} vs {before_high:.2f}" if after_high else "—",
        "有效突破前高")

    # 反转信号（D0）
    add(rules, "⑲", "D0收阳线", c[-1] > o[-1],
        f"收{c[-1]:.2f} 开{o[-1]:.2f}", "当日多头占优")
    if low_min is not None:
        reb = (c[-1] - low_min) / atr[big_idx]
        reb_ok = 1.0 <= reb <= 2.4
    else:
        reb, reb_ok = None, False
    add(rules, "⑳", "反弹幅度 1.0~2.4倍ATR",
        reb_ok, f"{reb:.2f}倍ATR" if reb is not None else "—",
        "反弹力度适中")
    add(rules, "㉑", "D0收盘 ≤ MA5 + 1.0×ATR",
        c[-1] <= ma[5][-1] + atr[-1],
        f"{c[-1]:.2f} vs {ma[5][-1]+atr[-1]:.2f}",
        "不追离均线太远")
    add(rules, "㉒", "D0最低 < 回调期最低点（深V）",
        low_min is not None and l[-1] < low_min,
        f"D0低 {l[-1]:.2f} vs 回调低 {low_min:.2f}" if low_min else "—",
        "盘中创回调新低后收回")

    return rules


# ============================================================
#  选股主流程
# ============================================================
STRATEGIES = {
    "trend": {"name": "趋势信号（31项）", "builder": _trend_rules,
              "risk": TREND_RISK},
    "reversal": {"name": "回调反转（22项）", "builder": _reversal_rules,
                 "risk": []},
}

# 形态模式下，各策略对应的必需标签
PATTERN_STRATEGY_TAG = {
    "ma_bull": "D均线多头",
    "breakout": "D放量突破",
    "pullback": "D回调企稳",
    "weekly_golden": "W周线金叉",
    "weekly_bull": "W周线多头",
    "weekly_breakout": "W周线突破",
}

DEFAULT_CONFIG = {
    "min_mv": 30.0,          # 总市值下限（亿元）
    "max_mv": 3000.0,        # 总市值上限（亿元）
    "price_min": 3.0,
    "price_max": 300.0,
    "min_amount": 0.5,       # 成交额下限（亿元）
    "scope": "all",          # all/main/gem/star/bj
    "max_candidates": 400,   # 0 = 全部（可能较慢）
    "min_tags": 2,           # 形态组合模式最低命中标签数
    "workers": 10,
    "with_weekly": True,
    "max_days": 520,
    "include_near_miss": True,   # 严格策略附带「差 1~2 条」的观察标的
}


def _normalize_config(cfg: Optional[dict]) -> dict:
    c = dict(DEFAULT_CONFIG)
    c.update(cfg or {})
    c["min_mv"] = max(0.0, float(c.get("min_mv", 30)))
    c["max_mv"] = max(c["min_mv"], float(c.get("max_mv", 3000)))
    c["max_candidates"] = max(0, int(c.get("max_candidates", 400)))
    c["min_tags"] = max(1, int(c.get("min_tags", 2)))
    c["workers"] = max(2, min(20, int(c.get("workers", 10))))
    return c


def _scope_ok(code: str, scope: str) -> bool:
    if scope == "main":
        return code.startswith("60") or code.startswith("00")
    if scope == "gem":
        return code.startswith("30")
    if scope == "star":
        return code.startswith("68")
    if scope == "bj":
        return code.startswith(("8", "4", "92"))
    return True


def _filter_candidates(quotes: dict, cfg: dict) -> tuple:
    """按市值/板块/价格/流动性/风险标签做扫描前硬过滤。"""
    kept = []
    stats = {"ST/退": 0, "新股": 0, "停牌": 0, "市值": 0, "价格": 0,
             "流动性": 0, "板块": 0}
    for code, q in quotes.items():
        if q.get("is_st"):
            stats["ST/退"] += 1
            continue
        if q.get("is_new") or q["name"].upper().startswith(("N", "C")):
            stats["新股"] += 1
            continue
        if q["price"] <= 0:
            stats["停牌"] += 1
            continue
        mv = q.get("total_mv") or q.get("circ_mv") or 0
        if mv <= 0 or not (cfg["min_mv"] <= mv <= cfg["max_mv"]):
            stats["市值"] += 1
            continue
        if not (cfg["price_min"] <= q["price"] <= cfg["price_max"]):
            stats["价格"] += 1
            continue
        if q.get("amount", 0) < cfg["min_amount"] * 1e8:
            stats["流动性"] += 1
            continue
        if not _scope_ok(code, cfg["scope"]):
            stats["板块"] += 1
            continue
        kept.append((code, q))
    kept.sort(key=lambda x: x[1].get("amount", 0), reverse=True)
    return kept, stats


def _analyze_pattern(code: str, q: dict, cfg: dict,
                     strategy: str) -> Optional[dict]:
    """形态选股：取日线 → 日线/周线形态标签 → 按策略过滤。"""
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=cfg["max_days"])) \
        .strftime("%Y%m%d")
    try:
        df = fetch_daily_fast(code, start, end)
    except Exception:
        return None
    if df is None or len(df) < 65:
        return None
    try:
        tags, details, f, wf, groups = pattern_tags(
            df, with_weekly=bool(cfg["with_weekly"]))
    except Exception:
        return None
    if not tags:
        return None

    daily_tags = groups.get("日线形态", [])
    weekly_tags = groups.get("周线形态", [])

    if strategy == "pattern":
        if len(tags) < cfg["min_tags"]:
            return None
    else:
        need = PATTERN_STRATEGY_TAG.get(strategy)
        if need and need not in tags:
            return None

    score = _pattern_score(tags)
    tag_count = len(tags)
    # 因子化加分：流动性/换手适中、市值适中，便于横向排序
    turnover = q.get("turnover", 0) or 0
    if 2 <= turnover <= 15:
        score += 4
    if 50 <= (q.get("total_mv") or 0) <= 800:
        score += 3
    q_amt = q.get("amount", 0) / 1e8
    if q_amt >= 2:
        score += 3

    return {
        "code": code, "name": q["name"], "price": q["price"],
        "change_pct": q["change_pct"], "total_mv": q.get("total_mv", 0),
        "circ_mv": q.get("circ_mv", 0), "turnover": turnover,
        "amount": q.get("amount", 0), "industry": q.get("industry", ""),
        "tags": tags, "daily_tags": daily_tags, "weekly_tags": weekly_tags,
        "tag_count": tag_count, "score": score, "details": details,
        "signal": True, "strategy": strategy,
        "pass_count": tag_count, "total": tag_count,
        "passed": [{"name": t, "detail": d} for t, d in zip(tags, details)],
        "failed": [], "mode": "pattern",
    }


def _analyze_strict(code: str, q: dict, strategy: str,
                    cfg: dict) -> Optional[dict]:
    """严格策略：完整跑原规则，返回通过/未通过明细。"""
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=cfg["max_days"])) \
        .strftime("%Y%m%d")
    try:
        df = fetch_daily_fast(code, start, end)
    except Exception:
        return None
    if df is None or len(df) < 260:
        return None
    try:
        ctx = precompute(df, q["price"], q.get("total_mv") or 0)
        rules = STRATEGIES[strategy]["builder"](ctx, code)
    except Exception:
        return None

    passed = [x for x in rules if x["ok"]]
    failed = [x for x in rules if not x["ok"]]
    near = len(failed) <= 2
    if len(failed) > 0 and not (cfg["include_near_miss"] and near):
        return None

    tags_hit, details, _, wf, groups = pattern_tags(df)
    score = int(len(passed) / max(1, len(rules)) * 100)
    return {
        "code": code, "name": q["name"], "price": q["price"],
        "change_pct": q["change_pct"], "total_mv": q.get("total_mv", 0),
        "circ_mv": q.get("circ_mv", 0),
        "turnover": q.get("turnover", 0) or 0,
        "amount": q.get("amount", 0), "industry": q.get("industry", ""),
        "tags": tags_hit, "daily_tags": groups.get("日线形态", []),
        "weekly_tags": groups.get("周线形态", []),
        "tag_count": len(tags_hit), "score": score, "details": details,
        "signal": len(failed) == 0, "strategy": strategy,
        "pass_count": len(passed), "total": len(rules),
        "passed": passed, "failed": failed, "mode": "strict",
    }


def _analyze(code: str, q: dict, strategy: str,
             cfg: dict) -> Optional[dict]:
    """统一分析入口（形态 / 严格）。"""
    if strategy in STRATEGY_NAMES and strategy not in STRATEGIES \
            and strategy != "all":
        return _analyze_pattern(code, q, cfg, strategy)
    return _analyze_strict(code, q, strategy, cfg)


def scan(strategy: str = "pattern",
         progress: Optional[Callable[[str], None]] = None,
         config: Optional[dict] = None,
         on_results: Optional[Callable[[list], None]] = None,
         stop_event=None,
         quotes: Optional[dict] = None) -> dict:
    """
    全市场（或按市值/板块范围）扫描选股。

    参数：
      strategy    : pattern / ma_bull / breakout / pullback /
                    weekly_golden / weekly_bull / weekly_breakout /
                    trend / reversal / all
      progress    : 进度回调（字符串）
      config      : 过滤与性能参数（见 DEFAULT_CONFIG）
      on_results  : 增量结果回调（每批命中列表，供界面边算边显示）
      stop_event  : threading.Event，置位后中断扫描并返回已得结果
      quotes      : 可复用已获取的全市场行情（避免重复请求）

    返回 dict：strategy/date/scanned/candidates/hits/risk/cancelled/elapsed。
    """
    cfg = _normalize_config(config)
    t0 = dt.datetime.now()

    def log(m):
        if progress:
            progress(m)

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    log("获取全市场行情（东财清单，失败自动降级腾讯…）")
    if quotes is None:
        try:
            quotes = fetch_all_quotes()
        except Exception as e:  # noqa
            return {"strategy": strategy, "hits": [], "scanned": 0,
                    "candidates": 0, "cancelled": False,
                    "error": f"行情获取失败：{e}", "risk": [],
                    "elapsed": 0.0}

    cands, stats = _filter_candidates(quotes, cfg)
    log(f"全市场 {len(quotes)} 只 → 过滤后 {len(cands)} 只"
        f"（剔除 ST/退 {stats['ST/退']}、新股 {stats['新股']}、"
        f"停牌 {stats['停牌']}、市值 {stats['市值']}、价格 {stats['价格']}、"
        f"流动性 {stats['流动性']}、板块 {stats['板块']}）")

    if cfg["max_candidates"] and len(cands) > cfg["max_candidates"]:
        cands = cands[:cfg["max_candidates"]]
        log(f"按成交额优先取前 {len(cands)} 只精筛（可在界面调整扫描上限）")

    # 严格策略需要跑 trend + reversal
    strict_list = []
    if strategy == "all":
        strict_list = ["trend", "reversal"]
    elif strategy in STRATEGIES:
        strict_list = [strategy]

    results: List[dict] = []
    done = 0
    total_tasks = len(cands) * max(1, len(strict_list)) \
        if strict_list else len(cands)
    last_emit = [0.0]

    def emit_batch(batch, force=False):
        now = dt.datetime.now().timestamp()
        if on_results and batch and (force or now - last_emit[0] >= 0.4):
            last_emit[0] = now
            try:
                on_results(batch)
            except Exception:
                pass

    def analyze_one(code, q):
        if strict_list:
            out = []
            for s in strict_list:
                r = _analyze_strict(code, q, s, cfg)
                if r:
                    out.append(r)
            return out
        r = _analyze_pattern(code, q, cfg, strategy)
        return [r] if r else []

    batch_acc: List[dict] = []
    cancelled = False
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = {ex.submit(analyze_one, code, q): code for code, q in cands}
        for fut in as_completed(futs):
            if stopped():
                cancelled = True
                for f in futs:
                    f.cancel()
                break
            done += 1
            try:
                got = fut.result() or []
            except Exception:
                got = []
            if got:
                results.extend(got)
                batch_acc.extend(got)
                if len(batch_acc) >= 12:
                    emit_batch(batch_acc)
                    batch_acc = []
            if done % 30 == 0 or done == total_tasks:
                log(f"已扫描 {done}/{total_tasks} · 命中 {len(results)}")
        emit_batch(batch_acc, force=True)

    results.sort(key=lambda x: (x.get("score", 0), x.get("change_pct", 0)),
                 reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i

    elapsed = (dt.datetime.now() - t0).total_seconds()
    risk = STRATEGIES[strategy]["risk"] if strategy in STRATEGIES else []
    log(f"扫描完成：命中 {len(results)} 只，耗时 {elapsed:.1f}s"
        + ("（已中断）" if cancelled else ""))
    return {
        "strategy": strategy,
        "strategy_name": STRATEGY_NAMES.get(strategy, strategy),
        "date": dt.date.today().strftime("%Y-%m-%d"),
        "scanned": done, "candidates": len(cands),
        "hits": results, "risk": risk, "cancelled": cancelled,
        "elapsed": round(elapsed, 1), "filter_stats": stats,
        "config": cfg,
    }


if __name__ == "__main__":
    res = scan("pattern", progress=lambda m: print(m),
               config={"max_candidates": 30, "min_tags": 2})
    print(f"\n==> 命中 {len(res['hits'])} 只 / 候选 {res['candidates']} 只")
    for r in res["hits"][:15]:
        print(f"#{r['rank']} {r['code']} {r['name']} 现价{r['price']} "
              f"市值{r['total_mv']:.0f}亿 涨{r['change_pct']:+.1f}% "
              f"形态分{r['score']} 标签:{'、'.join(r['tags'])}")

