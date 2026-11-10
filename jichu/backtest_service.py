# -*- coding: utf-8 -*-
"""
量化回测服务（双模式）
======================
模块六「量化回测」支持两种运行模式，均复用第五模块的机构策略近似权重：

1. 单一个股模式（run_single_stock）
   - 用户自选一只股票，按 8 因子时序打分做出买入/持有/卖出决策；
   - 仓位由综合分决定（20%~95%），信号收盘生成、次日开盘成交，杜绝未来函数；
   - 输出每日净值、仓位、买卖价格、数量、买卖原因、交易明细与绩效指标。

2. 全市场组合模式（run_universe）
   - 股票池来自全市场快照，剔除 ST/新股/低价/低流动性后按成交额排序；
   - 受免费数据源限制，默认只拉取流动性前 N 只（可调，界面会明确标注）；
   - 每 20 个交易日按多因子横截面打分调仓，等权目标仓位，行业上限来自策略预设；
   - 输出净值、仓位、逐笔买卖、调仓原因、止损记录与相对全市场等权基准的超额。

风险与限制：
  - 历史新闻/题材无法回溯，单票与组合的「信息面」均用行业动量、量价强度、
    相对强弱等价量代理，避免使用未来信息；
  - 全市场池为「当前存续股票」，存在幸存者偏差；
  - 机构真实因子权重不公开，本模块是公开方法论的近似实现，不构成投资建议。
"""

import datetime as dt
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import numpy as np
import pandas as pd

import strategy_library as SL

COMMISSION = 0.0003       # 佣金 万三（双边）
STAMP_TAX = 0.001        # 印花税 千一（卖出）
SLIPPAGE = 0.001         # 滑点 0.1%
RISK_FREE = 0.02         # 无风险利率（年化）
STOP_LOSS = 0.12         # 组合模式个股止损线：较成本回撤 12%
MAX_POSITION = 0.95      # 组合最大总仓位
IMPACT_COST = 0.0008     # 冲击成本系数（按订单占当日成交额比例）
MAX_PARTICIPATION = 0.05 # 单票单日最多成交当日成交量的 5%
FUND_WEIGHT = 0.15       # 资金面（龙虎榜+北向+席位）在总分中的占比
LHB_FULL = 0.02          # 净买额占流通市值 2% 记为满分
NORTH_FULL = 0.35        # 北向成交额占日均成交额 35% 记为满分


def _amt_str(v: float) -> str:
    """金额转“亿/万”可读文本。"""
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}元"


def _limit_pct(code: str, name: str = "") -> float:
    """按板块与 ST 标记返回涨跌停幅度。"""
    if "ST" in (name or "").upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


# ============================================================
#  基础工具
# ============================================================
def _clip01(x) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    if not np.isfinite(v):
        return 0.0
    return max(0.0, min(1.0, v))


def _clip_series(s: pd.Series) -> pd.Series:
    return s.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(0, 1)


def _today() -> pd.Timestamp:
    return pd.Timestamp(dt.date.today())


def _range_from_months(months: int) -> tuple:
    end = _today()
    start = end - pd.DateOffset(months=max(1, int(months)))
    return start.normalize(), end.normalize()


def _fetch_history(code: str, start: pd.Timestamp, end: pd.Timestamp,
                   warmup_days: int = 330) -> pd.DataFrame:
    """拉取日线并额外预留预热区间，用于 MA60/120 与 60 日动量。"""
    from data_sources import fetch_daily
    s = (start - pd.Timedelta(days=warmup_days)).strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    df = fetch_daily(code, s, e)
    if df is None or df.empty:
        raise ValueError(f"{code} 未获取到日线数据")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    return df.sort_index()


def _raw_metrics(hist: pd.DataFrame) -> Optional[dict]:
    """从截至某一日的日线窗口提取价量特征，不做任何未来数据引用。"""
    if hist is None or len(hist) < 100:
        return None
    c = hist["close"].to_numpy(dtype=float)
    h = hist["high"].to_numpy(dtype=float)
    l = hist["low"].to_numpy(dtype=float)
    v = hist["volume"].to_numpy(dtype=float)
    amt = hist["amount"].to_numpy(dtype=float)
    n = len(c)
    if n < 100 or c[-1] <= 0:
        return None

    def ret(k):
        return float(c[-1] / c[-1 - k] - 1) if n > k and c[-1 - k] > 0 else 0.0

    ma5 = float(np.mean(c[-5:]))
    ma10 = float(np.mean(c[-10:]))
    ma20 = float(np.mean(c[-20:]))
    ma60 = float(np.mean(c[-60:])) if n >= 60 else float(np.mean(c))
    vol_base = float(np.mean(v[-20:]))
    vol_ratio = float(np.mean(v[-5:]) / vol_base) if vol_base > 0 else 1.0
    amt_base = float(np.mean(amt[-20:]))
    amt_ratio = float(np.mean(amt[-5:]) / amt_base) if amt_base > 0 else 1.0
    avg_amount = float(np.mean(amt[-20:])) if n >= 20 else float(np.mean(amt))
    rets20 = np.diff(c[-21:]) / c[-21:-1] if n >= 21 else np.array([0.0])
    vol20 = float(np.std(rets20) * math.sqrt(252)) if len(rets20) else 0.0
    high20 = float(np.max(h[-20:]))
    low20 = float(np.min(l[-20:]))
    dd20 = float((high20 - c[-1]) / high20) if high20 > 0 else 0.0
    pos20 = float((c[-1] - low20) / (high20 - low20)) \
        if high20 > low20 else 0.5
    high120 = float(np.max(h[-120:])) if n >= 120 else float(np.max(h))
    dist120 = float(c[-1] / high120 - 1) if high120 > 0 else 0.0
    up_ratio = float(np.mean(np.diff(c[-21:]) > 0)) if n >= 21 else 0.5
    up_ratio = max(0.0, min(1.0, up_ratio))
    ma20_series = pd.Series(c).rolling(20).mean().to_numpy()
    slope20 = float(ma20_series[-1] / ma20_series[-6] - 1) \
        if n >= 26 and np.isfinite(ma20_series[-6]) and ma20_series[-6] > 0 else 0.0
    ma_bull = bool(ma5 > ma10 > ma20 and c[-1] > ma20)
    return {
        "close": float(c[-1]), "ret1": ret(1), "ret5": ret(5),
        "ret20": ret(20), "ret60": ret(60),
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "ma_bull": ma_bull, "slope20": slope20,
        "vol_ratio": vol_ratio, "up_ratio": up_ratio,
        "vol20": vol20, "dd20": dd20, "pos20": pos20,
        "dist120": dist120, "avg_amount": avg_amount,
        "amt_ratio": amt_ratio,
    }


def _factor_names() -> dict:
    return dict(SL.FACTOR_NAME)


def _factor_reason(factors: dict, top: int = 3) -> str:
    """把因子得分转成可读理由，按「得分/满分」贡献排序。"""
    names = _factor_names()
    ranked = sorted(
        factors.items(),
        key=lambda kv: (kv[1]["score"] / kv[1]["max"]) if kv[1]["max"] else 0,
        reverse=True)
    parts = []
    for k, v in ranked[:top]:
        parts.append(f"{names.get(k, k)} {v['score']}/{v['max']}（{v['note']}）")
    return "；".join(parts)


# ============================================================
#  单一个股模式：时序多因子打分
# ============================================================
def _single_factor_scores(m: dict, weights: dict) -> tuple:
    """单票模式：把价量特征映射到 8 因子，返回 (因子明细, 综合分)。"""
    c = m["close"]
    raw = {
        "trend": (0.20 * (c > m["ma5"]) + 0.20 * (m["ma5"] > m["ma10"])
                  + 0.20 * (m["ma10"] > m["ma20"])
                  + 0.10 * (m["ma20"] > m["ma60"])
                  + 0.15 * _clip01(m["ret20"] / 0.15)
                  + 0.15 * _clip01(max(m["slope20"], 0) / 0.05)),
        "volume": (0.35 * _clip01((m["vol_ratio"] - 0.8) / 1.2)
                   + 0.30 * m["up_ratio"]
                   + 0.35 * _clip01((m["amt_ratio"] - 0.9) / 1.1)),
        "liquidity": _clip01((math.log10(max(m["avg_amount"], 1)) - 7.0) / 2.0),
        # 单票没有板块指数，用 60 日趋势与 MA20 斜率近似“轮动/相对强度”
        "rotation": (0.50 * _clip01(m["ret60"] / 0.30)
                     + 0.50 * _clip01(max(m["slope20"], 0) / 0.05)),
        "leader": _clip01((m["ret20"] / max(m["vol20"], 0.10) + 2) / 4),
        "theme": (0.60 * _clip01((m["vol_ratio"] - 1) / 1.5)
                  + 0.40 * _clip01(m["ret5"] / 0.10)),
        "capital": (0.50 * m["pos20"]
                    + 0.50 * _clip01((m["amt_ratio"] - 1) / 1)),
        "risk": _clip01(1 - (0.40 * _clip01(m["vol20"] / 0.60)
                             + 0.35 * _clip01(m["dd20"] / 0.25)
                             + 0.25 * _clip01((m["ret20"] - 0.25) / 0.35))),
    }
    notes = {
        "trend": f"MA{'多头' if m['ma_bull'] else '震荡'}，20日{m['ret20']:+.1%}",
        "volume": f"量比{m['vol_ratio']:.2f}，上涨占比{m['up_ratio']:.0%}",
        "liquidity": f"20日均额{m['avg_amount'] / 1e8:.2f}亿",
        "rotation": f"60日{m['ret60']:+.1%}，MA20斜率{m['slope20']:+.1%}",
        "leader": f"20日强度{m['ret20'] / max(m['vol20'], 0.1):.2f}",
        "theme": f"5日{m['ret5']:+.1%}，量能{m['amt_ratio']:.2f}",
        "capital": f"区间位置{m['pos20']:.0%}，量能{m['amt_ratio']:.2f}",
        "risk": f"波动{m['vol20']:.1%}，20日回撤{m['dd20']:.1%}",
    }
    factors, total = {}, 0.0
    for k, w in weights.items():
        score = round(_clip01(raw.get(k, 0)) * w, 1)
        factors[k] = {"score": score, "max": round(w, 1),
                      "note": notes.get(k, "")}
        total += score
    return factors, round(total, 1)


def run_single_stock(code: str, months: int = 18, init_cash: float = 200_000,
                     strategy_id: str = "high_flyer",
                     entry_threshold: Optional[float] = None,
                     exit_threshold: Optional[float] = None,
                     min_hold_days: int = 5,
                     trail_stop: float = 0.13,
                     exec_constraints: bool = True,
                     fund_boost: bool = True,
                     progress: Optional[Callable[[str], None]] = None,
                     cancel: Optional[threading.Event] = None) -> dict:
    """单一个股择时回测：信号收盘生成，次日开盘成交。"""
    def log(msg):
        if progress:
            progress(msg)

    def stopped():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("回测已取消")

    strat = SL.get_strategy(strategy_id)
    weights = SL.validate_weights(strat["weights"])
    start, end = _range_from_months(months)
    log(f"单票回测：{code}，{start:%Y-%m-%d} ~ {end:%Y-%m-%d}，"
        f"策略 {strat['org']}")
    df = _fetch_history(code, start, end)
    if df.index[-1] < start + pd.Timedelta(days=10):
        raise ValueError("区间内无足够行情数据")
    sim_start = df.index[df.index >= start]
    if len(sim_start) < 20:
        raise ValueError("区间内交易日不足 20 天，请缩短/调整回测长度")
    i0 = int(df.index.get_indexer([sim_start[0]])[0])
    if i0 < 90:
        raise ValueError("预热数据不足（需至少 90 个交易日历史）")

    name, industry, circ_mv = "", "", 0.0
    try:
        from data_sources import fetch_all_quotes
        q = fetch_all_quotes().get(code)
        if q:
            name, industry = q.get("name", ""), q.get("industry", "")
            circ_mv = float(q.get("circ_mv") or 0.0) * 1e8  # 亿 → 元
    except Exception:
        pass

    fund_data = {}
    if fund_boost:
        try:
            from market_info import fund_flow_range
            fund_data = fund_flow_range(f"{start:%Y-%m-%d}",
                                        f"{end:%Y-%m-%d}", log)
            log(f"资金面：龙虎榜 {len(fund_data.get('lhb') or {})} 天，"
                f"北向 {len(fund_data.get('north') or {})} 天")
        except Exception as exc:
            fund_data = {}
            log(f"资金面数据获取失败（{exc}），本轮只用价量因子")

    seat_by_date = {}
    if fund_data:
        try:
            from market_info import seat_quality_range
            hit_days = [d for d, m in (fund_data.get("lhb") or {}).items()
                        if code in m]
            if hit_days:
                seat_by_date = seat_quality_range(hit_days, log)
                log(f"席位质量：覆盖 "
                    f"{len([d for d in hit_days if seat_by_date.get(d)])}"
                    f"/{len(hit_days)} 个上榜日")
        except Exception:
            seat_by_date = {}

    base_min = float(strat["params"].get("min_score", 40))
    entry_th = float(entry_threshold) if entry_threshold is not None \
        else max(60.0, base_min + 20)
    exit_th = float(exit_threshold) if exit_threshold is not None \
        else max(38.0, base_min - 2)
    max_hold_days = 120
    trail_stop = max(0.05, min(0.20, float(trail_stop)))

    cash = float(init_cash)
    shares = 0
    entry_price = 0.0
    entry_cost = 0.0
    entry_date = None
    peak_close = 0.0
    pending = None            # ("buy"/"sell", reason)
    curve, daily, trades = [], [], []
    fees_total = 0.0
    exec_stats = {"blocked_buy": 0, "blocked_sell": 0,
                  "capped_buy": 0, "impact_cost": 0.0}

    for i in range(i0, len(df)):
        stopped()
        date = df.index[i]
        o = float(df["open"].iloc[i])
        c = float(df["close"].iloc[i])
        action = "持仓" if shares > 0 else "空仓"
        note = ""

        # ---- 执行上一交易日收盘生成的信号（今日开盘价成交）----
        if pending:
            kind, reason, sig_score = pending
            prev_c = float(df["close"].iloc[i - 1]) if i > 0 else c
            lim = _limit_pct(code, name)
            vi = float(df["volume"].iloc[i]) if "volume" in df.columns else 0.0
            amt_i = float(df["amount"].iloc[i]) if "amount" in df.columns \
                else vi * o
            allow = True
            if exec_constraints and vi <= 0:
                allow = False
            elif exec_constraints and kind == "buy" \
                    and o >= prev_c * (1 + lim - 0.002):
                allow = False
            elif exec_constraints and kind == "sell" \
                    and o <= prev_c * (1 - lim + 0.002):
                allow = False
            if not allow:
                key = "blocked_buy" if kind == "buy" else "blocked_sell"
                exec_stats[key] += 1
                action = "空仓" if kind == "buy" else "持仓"
                note = "停牌/涨跌停导致委托未成交"
            elif kind == "buy" and shares == 0:
                # 仓位随综合分提高：60 分约 55%，80 分约 78%，95+ 接近满仓
                target = max(0.50, min(1.0,
                                       0.50 + (sig_score - 55) / 45 * 0.50))
                budget = cash * target
                imp = 0.0
                if exec_constraints and amt_i > 0:
                    imp = min(0.01, IMPACT_COST * (budget / amt_i)
                              / MAX_PARTICIPATION)
                buy_price = o * (1 + SLIPPAGE) * (1 + imp)
                lots = int(budget / (buy_price * (1 + COMMISSION)) // 100 * 100)
                if exec_constraints and vi > 0:
                    cap = int(vi * MAX_PARTICIPATION // 100 * 100)
                    if lots > cap:
                        lots = cap
                        exec_stats["capped_buy"] += 1
                if lots > 0:
                    cost = lots * buy_price
                    fee = cost * COMMISSION
                    cash -= cost + fee
                    shares = lots
                    entry_price, entry_cost = buy_price, cost + fee
                    entry_date = date
                    peak_close = max(buy_price, c)
                    fees_total += fee
                    exec_stats["impact_cost"] += lots * buy_price * imp
                    action = "买入"
                    note = (f"次日开盘 {buy_price:.2f} 买入 {lots} 股，"
                            f"仓位约 {target * 100:.0f}%，金额 {cost:,.0f}，"
                            f"佣金 {fee:,.0f}；买入理由：{reason}")
                    equity_now = cash + lots * buy_price
                    trades.append({
                        "date": date.strftime("%Y-%m-%d"), "code": code,
                        "name": name, "side": "买入",
                        "price": round(buy_price, 3), "shares": lots,
                        "amount": round(cost, 2),
                        "position_pct": round(lots * buy_price /
                                              max(equity_now, 1) * 100, 2),
                        "pnl": None, "return_pct": None,
                        "buy_date": "", "buy_price": None,
                        "reason": f"综合分 {sig_score:.1f}，"
                                  f"目标仓位 {target * 100:.0f}%；{reason}",
                        "fees": round(fee, 2),
                    })
                else:
                    action = "空仓"
                    note = "资金不足 1 手，买入未成交"
            elif kind == "sell" and shares > 0:
                imp = 0.0
                if exec_constraints and amt_i > 0:
                    imp = min(0.01, IMPACT_COST * (shares * o / amt_i)
                              / MAX_PARTICIPATION)
                sell_price = o * (1 - SLIPPAGE) * (1 - imp)
                revenue = shares * sell_price
                fee = revenue * (COMMISSION + STAMP_TAX)
                cash += revenue - fee
                fees_total += fee
                exec_stats["impact_cost"] += shares * sell_price * imp
                pnl = revenue - fee - entry_cost
                hold_days = (date - entry_date).days
                trades.append({
                    "date": date.strftime("%Y-%m-%d"), "code": code,
                    "name": name, "side": "卖出",
                    "price": round(sell_price, 3), "shares": shares,
                    "amount": round(revenue, 2), "position_pct": 0.0,
                    "pnl": round(pnl, 2),
                    "return_pct": round(pnl / entry_cost * 100, 2)
                    if entry_cost else 0.0,
                    "buy_date": entry_date.strftime("%Y-%m-%d"),
                    "buy_price": round(entry_price, 3),
                    "reason": reason, "fees": round(fee, 2),
                })
                action = "卖出"
                note = (f"次日开盘 {sell_price:.2f} 卖出 {shares} 股，"
                        f"税费 {fee:,.0f}，盈亏 {pnl:+,.0f}；卖出理由：{reason}")
                shares, entry_price, entry_cost, entry_date = 0, 0.0, 0.0, None
                peak_close = 0.0
            pending = None

        # ---- 收盘计算因子与下一日信号 ----
        hist_now = df.iloc[:i + 1].tail(180)
        m = _raw_metrics(hist_now)
        fm, score = _single_factor_scores(m, weights) if m else ({}, 0.0)
        if m and fund_data:
            ds = date.strftime("%Y-%m-%d")
            net = float((fund_data.get("lhb") or {}).get(ds, {}).get(code, 0))
            n_amt = float((fund_data.get("north") or {}).get(ds, {}).get(code, 0))
            day_amt = float(df["amount"].iloc[i]) \
                if "amount" in df.columns and np.isfinite(df["amount"].iloc[i]) \
                else 0.0
            q = float((seat_by_date.get(ds) or {}).get(code, 0.0))
            # 席位质量 ∈ [-1,1]，按 0.8 权重放大净买额（买入席位历史胜率高则更强）
            eff = net * (1.0 + 0.8 * max(-1.5, min(1.5, q)))
            lhb_s = _clip01(0.5 + (eff / circ_mv * 20 if circ_mv > 0 else 0.0)) \
                if net else 0.0
            north_s = _clip01(n_amt / day_amt * 3) if (n_amt and day_amt) else 0.0
            fund_s = 0.5 * lhb_s + 0.5 * north_s
            # 纯加分，未出现资金面信号时不改变原分数
            score = score + fund_s * 100 * FUND_WEIGHT
            half = round(100 * FUND_WEIGHT * 0.5, 1)
            fm["lhb"] = {"score": round(lhb_s * half, 1), "max": half,
                         "pct": round(lhb_s, 4),
                         "note": (("龙虎榜净买 " + _amt_str(net)) if net > 0
                                  else "当日未上榜")
                                 + (f"｜席位质量 {q:+.2f}" if net > 0 and q else "")}
            fm["north"] = {"score": round(north_s * half, 1), "max": half,
                           "pct": round(north_s, 4),
                           "note": ("北向成交 " + _amt_str(n_amt)) if n_amt > 0
                                   else "未进北向前十"}
        signal_note = ""
        if m:
            signal_note = f"综合分 {score:.1f}"
            if shares > 0:
                hold_days = (date - entry_date).days
                peak_close = max(peak_close, c)
                stop = c < entry_price * (1 - STOP_LOSS)
                trail = (peak_close > entry_price * 1.03
                         and c < peak_close * (1 - trail_stop))
                ma_break = (hold_days >= min_hold_days
                            and c < m["ma20"] * 0.985 and m["ret20"] < 0)
                exit_sig = hold_days >= min_hold_days and score < exit_th
                timeout = hold_days > max_hold_days
                if stop or trail or ma_break or exit_sig or timeout:
                    why = ("止损：收盘较买入价回撤超过 12%" if stop else
                           f"移动止损：较持有期高点回撤超过 {trail_stop:.0%}" if trail else
                           "趋势破位：收盘跌破 MA20 且 20 日动量为负" if ma_break else
                           f"综合分 {score:.1f} 低于卖出阈值 {exit_th:.0f}" if exit_sig
                           else f"持有超过 {max_hold_days} 天")
                    pending = ("sell", f"{why}；{_factor_reason(fm, 2)}", score)
            elif score >= entry_th:
                pending = ("buy", f"综合分 {score:.1f}≥{entry_th:.0f}；"
                                  f"{_factor_reason(fm, 3)}", score)
        if not note:
            note = signal_note + ("；等待买入信号" if shares == 0 else "；继续持有")

        total = cash + shares * c
        mv = shares * c
        pos_pct = round(mv / total * 100, 2) if total else 0.0
        bench = float(init_cash * (c / float(df["close"].iloc[i0])))
        curve.append({
            "date": date.strftime("%Y-%m-%d"), "nav": round(total, 2),
            "bench": round(bench, 2), "position_pct": pos_pct,
            "close": round(c, 3), "score": score,
        })
        daily.append({
            "date": date.strftime("%Y-%m-%d"), "action": action,
            "code": code, "name": name,
            "price": round(c, 3), "shares": shares,
            "cash": round(cash, 2), "total": round(total, 2),
            "position_pct": pos_pct, "note": note, "score": score,
        })

    # 未平仓头寸也记入交易明细（不计入胜率）
    if shares > 0 and entry_date is not None:
        last_close = float(df["close"].iloc[-1])
        unreal = shares * last_close - entry_cost
        trades.append({
            "date": df.index[-1].strftime("%Y-%m-%d"), "code": code,
            "name": name, "side": "持仓（未平）",
            "price": round(last_close, 3), "shares": shares,
            "amount": round(shares * last_close, 2),
            "position_pct": round(shares * last_close /
                                  max(cash + shares * last_close, 1) * 100, 2),
            "pnl": round(unreal, 2),
            "return_pct": round(unreal / entry_cost * 100, 2)
            if entry_cost else 0.0,
            "buy_date": entry_date.strftime("%Y-%m-%d"),
            "buy_price": round(entry_price, 3),
            "reason": "回测结束仍持有（按最新收盘价估值）", "fees": 0.0,
        })

    curve_df = pd.DataFrame(curve)
    closed = [t["pnl"] for t in trades if t["side"] == "卖出"]
    metrics = _metrics(curve_df, init_cash, closed, fees_total)
    return {
        "mode": "single", "mode_name": "单一个股·择时交易",
        "strategy_id": strategy_id, "strategy_name": strat["name"],
        "strategy_org": strat["org"], "code": code, "name": name,
        "industry": industry, "start": curve[0]["date"], "end": curve[-1]["date"],
        "init_cash": init_cash, "months": months,
        "entry_threshold": entry_th, "exit_threshold": exit_th,
        "curve": curve, "daily": daily, "trades": trades,
        "exec_stats": exec_stats, "exec_constraints": exec_constraints,
        "fund_boost": bool(fund_data), "fund_weight": FUND_WEIGHT,
        "metrics": metrics, "bench_label": "买入持有",
        "factor_meta": {k: round(v, 1) for k, v in weights.items()},
        "disclaimer": "单票模式无历史新闻/题材数据，信息面用量价与相对强度代理。",
    }


# ============================================================
#  全市场组合模式
# ============================================================
def _load_pool(quotes: dict, pool_size: int,
               max_price: Optional[float] = None,
               exclude_star_bse: bool = False) -> list:
    """从全市场快照筛选可交易股票，按成交额/流通市值排序。

    max_price 不为空时剔除股价高于该值的标的（默认不过滤）。
    exclude_star_bse=True 时剔除科创板（688/689）与北交所（8/4/9 开头）。
    """
    pool = []
    for q in quotes.values():
        code = str(q.get("code", ""))
        if not (len(code) == 6 and code.isdigit()):
            continue
        if exclude_star_bse and (
                code.startswith(("688", "689")) or
                code.startswith(("4", "8", "9"))):
            continue
        if q.get("is_st") or q.get("is_new"):
            continue
        px = float(q.get("price") or 0)
        if px < 2.0:
            continue
        if max_price is not None and px > max_price:
            continue
        if float(q.get("amount") or 0) < 20_000_000:   # 成交额 < 2000 万
            continue
        pool.append(q)
    pool.sort(key=lambda x: (float(x.get("amount") or 0),
                             float(x.get("circ_mv") or 0)), reverse=True)
    if pool_size and pool_size > 0:
        pool = pool[:pool_size]
    return pool


def _fetch_pool_daily(pool: list, start: pd.Timestamp, end: pd.Timestamp,
                      progress: Callable, cancel: Optional[threading.Event]) -> dict:
    """并发拉取股票池日线（带缓存），单只失败不影响整体。"""
    from data_sources import fetch_daily_fast
    s = (start - pd.Timedelta(days=330)).strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    data, failed = {}, []
    total = len(pool)

    def one(q):
        code = q["code"]
        try:
            df = fetch_daily_fast(code, s, e)
            if df is None or len(df) < 130:
                return code, None, "数据不足 130 根"
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            return code, df.sort_index(), ""
        except Exception as exc:
            return code, None, f"{type(exc).__name__}: {exc}"

    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(one, q): q for q in pool}
        for fut in as_completed(futs):
            if cancel is not None and cancel.is_set():
                for f in futs:
                    f.cancel()
                raise InterruptedError("回测已取消")
            code, df, err = fut.result()
            done += 1
            if df is not None:
                data[code] = df
            else:
                failed.append((code, err))
            if done % 20 == 0 or done == total:
                progress(f"加载股票日线 {done}/{total}，成功 {len(data)} 只…")
    if failed:
        progress(f"共 {len(failed)} 只拉取失败，已自动跳过（不影响整体）")
    return data


def _score_universe_at(date: pd.Timestamp, data: dict, quotes: dict,
                       weights: dict, neutralize: bool = False,
                       fund: Optional[dict] = None) -> tuple:
    """某一交易日收盘后，对股票池做横截面多因子打分。

    neutralize=True 时做行业 + 市值中性化：
    个股类因子先在同一行业内排百分位（行业样本 <6 时退回全池），
    再用市值分位做线性回归剔除规模暴露，最后重新排分位；
    行业动量/题材类因子保留行业信号，只做市值中性。
    """
    recs = []
    for code, df in data.items():
        hist = df.loc[:date].tail(190)
        if len(hist) < 120:
            continue
        if (date - hist.index[-1]).days > 15:
            continue
        m = _raw_metrics(hist)
        if not m:
            continue
        q = quotes.get(code, {})
        recs.append({
            "code": code, "name": q.get("name", ""),
            "industry": q.get("industry") or "未分类",
            "circ_mv": float(q.get("circ_mv") or 0.0), **m,
        })
    if not recs:
        return [], 0

    f = pd.DataFrame(recs)
    # 行业动量：该行业当日成分股 20 日/5 日平均收益的横截面分位
    ind20 = f.groupby("industry")["ret20"].mean().rank(pct=True)
    ind5 = f.groupby("industry")["ret5"].mean().rank(pct=True)
    f["ind20_pct"] = f["industry"].map(ind20).fillna(0.5)
    f["ind5_pct"] = f["industry"].map(ind5).fillna(0.5)

    f["trend_raw"] = (0.20 * (f["close"] > f["ma5"]).astype(float)
                      + 0.20 * (f["ma5"] > f["ma10"]).astype(float)
                      + 0.20 * (f["ma10"] > f["ma20"]).astype(float)
                      + 0.10 * (f["ma20"] > f["ma60"]).astype(float)
                      + 0.15 * _clip_series(f["ret20"] / 0.15)
                      + 0.15 * _clip_series(f["slope20"] / 0.05))
    f["volume_raw"] = (0.35 * _clip_series((f["vol_ratio"] - 0.8) / 1.2)
                       + 0.30 * f["up_ratio"]
                       + 0.35 * _clip_series((f["amt_ratio"] - 0.9) / 1.1))
    f["liquidity_raw"] = np.log10(f["avg_amount"].clip(lower=1))
    f["rotation_raw"] = (0.70 * f["ind20_pct"]
                         + 0.30 * _clip_series(f["ret60"] / 0.30))
    f["leader_raw"] = f["ret20"]
    f["theme_raw"] = (0.60 * f["ind5_pct"]
                      + 0.40 * _clip_series((f["vol_ratio"] - 1) / 1.5))
    f["capital_raw"] = (0.50 * f["pos20"]
                        + 0.50 * _clip_series((f["amt_ratio"] - 1) / 1.0))
    f["risk_raw"] = 1 - (0.45 * _clip_series(f["vol20"] / 0.60)
                         + 0.35 * _clip_series(f["dd20"] / 0.25)
                         + 0.20 * _clip_series((f["ret20"] - 0.25) / 0.35))

    # 规模分位：用于市值中性化
    size_pct = pd.Series(0.5, index=f.index)
    if f["circ_mv"].notna().sum() >= 10 and f["circ_mv"].nunique() > 5:
        size_pct = pd.to_numeric(f["circ_mv"], errors="coerce") \
            .rank(pct=True).fillna(0.5)

    stock_factors = {"trend", "volume", "liquidity",
                     "leader", "capital", "risk"}
    for k in weights:
        raw = f[k + "_raw"]
        if not neutralize:
            f[k + "_pct"] = raw.rank(pct=True, method="average").fillna(0.5)
            continue
        if k in stock_factors:
            cnt = raw.groupby(f["industry"]).transform("count")
            pct = raw.groupby(f["industry"]).rank(pct=True, method="average")
            pct = pct.where(cnt >= 6, raw.rank(pct=True, method="average"))
        else:
            pct = raw.rank(pct=True, method="average")
        pct = pct.fillna(0.5)
        x = size_pct.to_numpy(dtype=float) - 0.5
        y = pct.to_numpy(dtype=float)
        denom = float(np.dot(x, x))
        if denom > 1e-12:
            beta = float(np.dot(x, y) / denom)
            beta = max(-0.5, min(0.5, beta))
            pct = pd.Series(y - beta * x, index=f.index) \
                .rank(pct=True, method="average")
        f[k + "_pct"] = pct
    f["total"] = 0.0
    for k, w in weights.items():
        f["total"] += f[k + "_pct"] * w

    # 资金面增强：龙虎榜净买额占流通市值 + 北向成交额占成交额
    if fund:
        lhb_map = fund.get("lhb") or {}
        north_map = fund.get("north") or {}
        lhb_net = f["code"].map(lambda c: float(lhb_map.get(c, 0.0)))
        north_amt = f["code"].map(lambda c: float(north_map.get(c, 0.0)))
        # 流通市值单位是“亿”，龙虎榜净买额单位是“元”，统一换算成元
        circ = pd.to_numeric(f["circ_mv"], errors="coerce") * 1e8
        lhb_ratio = (lhb_net / circ.where(circ > 0)).fillna(0.0)
        avg_amt = pd.to_numeric(f["avg_amount"], errors="coerce")
        north_ratio = (north_amt / avg_amt.where(avg_amt > 0)).fillna(0.0)
        # 席位质量加权：买入席位历史胜率高则放大净买额，低则缩小。
        # 东财“上榜后3日胜率”算出的质量绝对值差异很小（多在 ±0.05），
        # 因此按当日横截面标准差归一化，让游资差异真正参与排序。
        if fund.get("seat"):
            seat_map = fund.get("seat") or {}
            seat_q = f["code"].map(lambda c: float(seat_map.get(c, 0.0)))
            active = seat_q[seat_q != 0]
            sd = float(active.std()) if len(active) > 2 else 0.0
            if sd > 1e-6:
                seat_q = (seat_q / sd).clip(-2, 2) / 2.0
            lhb_raw = lhb_ratio * (1.0 + 0.8 * seat_q)
        else:
            lhb_raw = lhb_ratio
        lhb_raw = lhb_raw.where(lhb_net > 0, 0.0)
        north_raw = north_ratio.where(north_amt > 0, 0.0)
        # 绝对刻度而非分位：净买额占流通市值 2% 记满分，北向成交占日均 35% 记满分。
        # 分位法在股票池里只有 1~2 只上榜股时永远给满分，席位差异无法体现。
        f["lhb_pct"] = (lhb_raw / LHB_FULL).clip(0.0, 1.0)
        f["north_pct"] = (north_raw / NORTH_FULL).clip(0.0, 1.0)
        f["fund"] = 0.5 * f["lhb_pct"] + 0.5 * f["north_pct"]
        # 纯加分：未上榜/未进前十 = 0 分，不改变原来的分数刻度
        f["total"] = f["total"] + f["fund"] * 100 * FUND_WEIGHT
    f["total"] = f["total"].round(1)

    notes = {
        "trend": lambda r: f"MA{'多头' if r['ma_bull'] else '震荡'}，"
                           f"20日{r['ret20']:+.1%}",
        "volume": lambda r: f"量比{r['vol_ratio']:.2f}，"
                            f"上涨占比{r['up_ratio']:.0%}",
        "liquidity": lambda r: f"20日均额{r['avg_amount'] / 1e8:.2f}亿",
        "rotation": lambda r: f"行业{r['industry']}，动量分位"
                              f"{r['ind20_pct']:.0%}",
        "leader": lambda r: f"20日涨幅{r['ret20']:+.1%}",
        "theme": lambda r: f"行业5日分位{r['ind5_pct']:.0%}，"
                           f"量能{r['amt_ratio']:.2f}",
        "capital": lambda r: f"区间位置{r['pos20']:.0%}，"
                             f"量能{r['amt_ratio']:.2f}",
        "risk": lambda r: f"波动{r['vol20']:.1%}，回撤{r['dd20']:.1%}",
    }
    out = []
    for _, r in f.iterrows():
        factors = {
            k: {"score": round(float(r[k + "_pct"]) * weights[k], 1),
                "max": round(weights[k], 1),
                "pct": round(float(r[k + "_pct"]), 4),
                "note": notes[k](r) if k in notes else ""}
            for k in weights
        }
        if fund:
            half = round(100 * FUND_WEIGHT * 0.5, 1)
            lhb_net = float((fund.get("lhb") or {}).get(r["code"], 0.0))
            n_amt = float((fund.get("north") or {}).get(r["code"], 0.0))
            seat_q = float((fund.get("seat") or {}).get(r["code"], 0.0)) \
                if fund.get("seat") else None
            factors["lhb"] = {
                "score": round(float(r["lhb_pct"]) * half, 1), "max": half,
                "pct": round(float(r["lhb_pct"]), 4),
                "note": (("龙虎榜净买 " + _amt_str(lhb_net))
                          if lhb_net > 0 else "当日未上榜")
                         + (f"｜席位质量 {seat_q:+.2f}" if seat_q is not None
                            and lhb_net > 0 else ""),
            }
            factors["north"] = {
                "score": round(float(r["north_pct"]) * half, 1), "max": half,
                "pct": round(float(r["north_pct"]), 4),
                "note": ("北向成交 " + _amt_str(n_amt))
                          if n_amt > 0 else "未进北向前十",
            }
        out.append({
            "code": r["code"], "name": r["name"],
            "industry": r["industry"], "total": float(r["total"]),
            "factors": factors, "reason": _factor_reason(factors, 3),
            "ret20": float(r["ret20"]),
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out, len(f)


def _universe_bench(close_mat: pd.DataFrame, init_cash: float) -> list:
    """全市场等权基准：每日等权再平衡，不含交易费用。"""
    rets = close_mat.pct_change().mean(axis=1).fillna(0.0)
    nav = (1 + rets).cumprod().to_numpy(dtype=float)
    return [round(init_cash * x, 2) for x in nav]


def run_universe(months: int = 18, init_cash: float = 200_000,
                 strategy_id: str = "high_flyer", top_n: int = 20,
                 pool_size: int = 400, rebalance_days: int = 20,
                 neutralize: bool = True, exec_constraints: bool = True,
                 fund_boost: bool = True,
                 max_position: float = MAX_POSITION,
                 max_price: Optional[float] = None,
                 exclude_star_bse: bool = False,
                 progress: Optional[Callable[[str], None]] = None,
                 cancel: Optional[threading.Event] = None) -> dict:
    """全市场组合回测：多因子横截面选股 + 定期调仓 + 个股止损。"""
    def log(msg):
        if progress:
            progress(msg)

    def stopped():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("回测已取消")

    strat = SL.get_strategy(strategy_id)
    weights = SL.validate_weights(strat["weights"])
    industry_cap = int(strat["params"].get("industry_cap", 3))
    min_score = float(strat["params"].get("min_score", 40))
    start, end = _range_from_months(months)

    log("正在获取全市场行情快照…")
    from data_sources import fetch_all_quotes
    quotes = fetch_all_quotes()
    pool = _load_pool(quotes, pool_size, max_price, exclude_star_bse)
    # 股票池上限可选 20/50/100/400，最小池为 20 只，阈值相应下调到 10
    min_pool = min(10, pool_size) if pool_size else 10
    if len(pool) < min_pool:
        raise ValueError(
            f"可用股票池仅 {len(pool)} 只（< {min_pool}），请检查行情源或放宽筛选")
    stopped()
    log(f"全市场 {len(quotes)} 只 → 筛选后 {len(pool)} 只，开始加载日线…")
    data = _fetch_pool_daily(pool, start, end, log, cancel)
    min_data = min_pool
    if len(data) < min_data:
        raise ValueError(f"仅成功加载 {len(data)} 只，样本过少，无法回测")

    fund_data = {}
    if fund_boost:
        try:
            from market_info import fund_flow_range
            fund_data = fund_flow_range(f"{start:%Y-%m-%d}",
                                        f"{end:%Y-%m-%d}", log)
            log(f"资金面：龙虎榜覆盖 {len(fund_data.get('lhb') or {})} 个交易日，"
                f"北向覆盖 {len(fund_data.get('north') or {})} 个交易日")
        except Exception as exc:
            fund_data = {}
            log(f"资金面数据获取失败（{exc}），本轮只用价量因子")

    # 交易日历：股票池日期并集（覆盖停牌造成的缺口）
    all_dates = sorted({d for df in data.values() for d in df.index})
    dates = pd.DatetimeIndex([d for d in all_dates if start <= d <= end])
    if len(dates) < 30:
        raise ValueError("区间内交易日不足 30 天")
    close_mat = pd.DataFrame({c: df["close"] for c, df in data.items()}) \
        .reindex(dates).ffill()
    open_mat = pd.DataFrame({c: df["open"] for c, df in data.items()}) \
        .reindex(dates)   # 执行价不做前向填充，停牌日不可成交
    vol_mat = pd.DataFrame({c: df["volume"] for c, df in data.items()}) \
        .reindex(dates)
    amt_mat = pd.DataFrame({
        c: (df["amount"] if "amount" in df.columns
            else df["close"] * df["volume"])
        for c, df in data.items()
    }).reindex(dates)
    prev_close_mat = close_mat.shift(1)
    bench_values = _universe_bench(close_mat, init_cash)

    cash = float(init_cash)
    holdings = {}          # code -> {shares, cost, buy_date}
    last_px = {}           # 截至当前交易日已知的最新收盘价（防未来函数）
    pending_target = None  # 下一交易日调仓目标；None 表示无调仓
    forced_sell = set()
    curve, daily, trades, positions = [], [], [], []
    fees_total = 0.0
    exec_stats = {"blocked_buy": 0, "blocked_sell": 0,
                  "capped_buy": 0, "impact_cost": 0.0}

    def open_price(code, date):
        try:
            v = float(open_mat.at[date, code])
            return v if np.isfinite(v) and v > 0 else None
        except Exception:
            return None

    def bar_vol(code, date):
        try:
            v = float(vol_mat.at[date, code])
            return v if np.isfinite(v) and v > 0 else None
        except Exception:
            return None

    def blocked(code, date, side, px):
        """涨跌停、停牌或当日无成交量时不可成交。"""
        if not exec_constraints:
            return False
        if bar_vol(code, date) is None:
            return True
        try:
            prev = float(prev_close_mat.at[date, code])
        except Exception:
            prev = np.nan
        if np.isfinite(prev) and prev > 0:
            lim = _limit_pct(code, quotes.get(code, {}).get("name", ""))
            if side == "买入" and px >= prev * (1 + lim - 0.002):
                return True
            if side == "卖出" and px <= prev * (1 - lim + 0.002):
                return True
        return False

    def impact_price(code, date, px, shares, side):
        """按订单占当日成交额比例加冲击成本。"""
        if not exec_constraints:
            return px, 0.0
        try:
            amt = float(amt_mat.at[date, code])
        except Exception:
            amt = 0.0
        if amt > 0 and shares > 0:
            part = shares * px / amt
            imp = min(0.01, IMPACT_COST * part / MAX_PARTICIPATION)
            if side == "买入":
                return px * (1 + imp), shares * px * imp
            return px * (1 - imp), shares * px * imp
        return px, 0.0

    def sell(code, date, price, reason):
        nonlocal cash, fees_total
        h = holdings.get(code)
        if not h or h["shares"] <= 0:
            return
        if blocked(code, date, "卖出", price):
            exec_stats["blocked_sell"] += 1
            return
        sh = h["shares"]
        price, imp_cost = impact_price(code, date, price, sh, "卖出")
        exec_stats["impact_cost"] += imp_cost
        gross = sh * price
        fee = gross * (COMMISSION + STAMP_TAX)
        cash += gross - fee
        fees_total += fee
        pnl = gross - fee - h["cost"]
        trades.append({
            "date": date.strftime("%Y-%m-%d"), "code": code,
            "name": quotes.get(code, {}).get("name", ""), "side": "卖出",
            "price": round(price, 3), "shares": sh,
            "amount": round(gross, 2), "position_pct": 0.0,
            "pnl": round(pnl, 2),
            "return_pct": round(pnl / h["cost"] * 100, 2)
            if h["cost"] else 0.0,
            "buy_date": h["buy_date"].strftime("%Y-%m-%d"),
            "buy_price": round(h["cost"] / sh, 3),
            "reason": reason, "fees": round(fee, 2),
        })
        del holdings[code]

    def buy(code, date, price, shares, reason, rank, total):
        nonlocal cash, fees_total
        if blocked(code, date, "买入", price):
            exec_stats["blocked_buy"] += 1
            return False
        v = bar_vol(code, date)
        if v:
            cap = int(v * MAX_PARTICIPATION // 100 * 100)
            if shares > cap:
                shares = cap
                exec_stats["capped_buy"] += 1
        if shares <= 0:
            return False
        price, imp_cost = impact_price(code, date, price, shares, "买入")
        exec_stats["impact_cost"] += imp_cost
        cost = shares * price
        fee = cost * COMMISSION
        need = cost + fee
        if need > cash:
            shares = int(cash / (price * (1 + COMMISSION)) // 100 * 100)
            if shares <= 0:
                return False
            cost = shares * price
            fee = cost * COMMISSION
            need = cost + fee
        cash -= need
        fees_total += fee
        h = holdings.get(code)
        if h:
            h["cost"] += need
            h["shares"] += shares
        else:
            holdings[code] = {"shares": shares, "cost": need,
                              "buy_date": date}
        trades.append({
            "date": date.strftime("%Y-%m-%d"), "code": code,
            "name": quotes.get(code, {}).get("name", ""), "side": "买入",
            "price": round(price, 3), "shares": shares,
            "amount": round(cost, 2),
            "position_pct": round(need / max(init_cash, 1) * 100, 2),
            "pnl": None, "return_pct": None, "buy_date": "",
            "buy_price": None,
            "reason": f"多因子排名 {rank}；综合分 {total:.1f}；{reason}",
            "fees": round(fee, 2),
        })
        return True

    def execute(date, target, do_rebalance):
        """在开盘执行调仓/止损；target 为 None 时只处理强制卖出。"""
        target = target or []
        target_codes = {r["code"] for r in target} if do_rebalance else set()
        equity = cash
        for code, h in holdings.items():
            px = open_price(code, date) or last_px.get(code) or 0
            equity += h["shares"] * px

        # 1) 止损优先
        for code in list(forced_sell):
            px = open_price(code, date)
            if px and code in holdings:
                sell(code, date, px * (1 - SLIPPAGE),
                     f"止损：收盘价较持仓成本回撤超过 {STOP_LOSS:.0%}")
        # 2) 调出不在目标的持仓
        if do_rebalance:
            for code in list(holdings):
                if code in target_codes:
                    continue
                px = open_price(code, date)
                if px:
                    sell(code, date, px * (1 - SLIPPAGE),
                         "调出组合：未进入本期多因子目标名单")
        if not do_rebalance:
            return

        # 3) 目标等权仓位：先减后加
        if not target:
            return
        target_value = equity * max_position / len(target)
        for r in target:
            code = r["code"]
            px = open_price(code, date)
            if not px:
                continue
            desired = int(target_value / (px * (1 + COMMISSION)) // 100 * 100)
            cur = holdings.get(code, {}).get("shares", 0)
            if cur > desired:
                delta = cur - desired
                if delta * px > equity * 0.005:
                    sell(code, date, px * (1 - SLIPPAGE),
                         f"权重再平衡：减持至目标仓位；{r['reason']}")
            elif cur < desired:
                delta = desired - cur
                if delta * px > equity * 0.005:
                    buy(code, date, px * (1 + SLIPPAGE), delta,
                         r["reason"], target.index(r) + 1, r["total"])

    seat_maps = {}
    if fund_data:
        try:
            from market_info import seat_quality_range
            rebal = [dates[i].strftime("%Y-%m-%d")
                     for i in range(0, len(dates), max(1, rebalance_days))]
            seat_maps = seat_quality_range(rebal, log)
            hit = len([d for d in rebal if seat_maps.get(d)])
            log(f"席位质量：覆盖 {hit}/{len(rebal)} 个调仓日")
        except Exception:
            seat_maps = {}

    signal_marks = []        # 记录每次调仓信号的下标，用于推算下次调仓日
    for i, date in enumerate(dates):
        stopped()
        executed = []
        if pending_target is not None or forced_sell:
            before = len(trades)
            execute(date, pending_target, pending_target is not None)
            executed = trades[before:]
            pending_target = None
            forced_sell = set()

        # 收盘估值（只使用截至当日已知收盘价，停牌沿用上一有效价）
        equity = cash
        for code, h in holdings.items():
            raw = close_mat.at[date, code]
            if pd.notna(raw) and float(raw) > 0:
                last_px[code] = float(raw)
            px = last_px.get(code)
            if px:
                equity += h["shares"] * px
                if px < (h["cost"] / h["shares"]) * (1 - STOP_LOSS):
                    forced_sell.add(code)
        mv = equity - cash
        pos_pct = round(mv / equity * 100, 2) if equity else 0.0
        curve.append({
            "date": date.strftime("%Y-%m-%d"), "nav": round(equity, 2),
            "bench": bench_values[i], "position_pct": pos_pct,
        })
        if executed:
            buys = sum(1 for t in executed if t["side"] == "买入")
            sells = sum(1 for t in executed if t["side"] == "卖出")
            action = f"买入{buys}/卖出{sells}"
            # 调仓/止损日逐笔列出个股：代码、名称、价格、数量、仓位与原因
            for t in executed:
                daily.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "action": t["side"], "code": t["code"],
                    "name": t["name"], "price": t["price"],
                    "shares": t["shares"], "cash": round(cash, 2),
                    "total": round(equity, 2),
                    "position_pct": round(t["amount"] / equity * 100, 2)
                    if equity else 0.0,
                    "holdings": len(holdings), "note": t["reason"],
                    "score": None,
                })
        else:
            held = list(holdings)
            if held:
                names = "、".join(
                    f"{c} {quotes.get(c, {}).get('name', '')}".strip()
                    for c in held[:8])
                if len(held) > 8:
                    names += "…"
                note = f"持有 {len(held)} 只：{names}；现金 {cash:,.0f}"
                action = "持仓"
            else:
                action, note = "空仓", "空仓等待信号"
            daily.append({
                "date": date.strftime("%Y-%m-%d"), "action": action,
                "code": "", "name": "", "price": None, "shares": None,
                "cash": round(cash, 2), "total": round(equity, 2),
                "position_pct": pos_pct, "holdings": len(holdings),
                "note": note, "score": None,
            })

        # 调仓日与期末保存持仓快照，便于查看“持有但未操作”的个股信息
        if executed or i == len(dates) - 1:
            for code, h in holdings.items():
                hpx = last_px.get(code) or (h["cost"] / h["shares"])
                val = h["shares"] * hpx
                positions.append({
                    "date": date.strftime("%Y-%m-%d"), "code": code,
                    "name": quotes.get(code, {}).get("name", ""),
                    "shares": h["shares"], "price": round(hpx, 3),
                    "value": round(val, 2),
                    "weight": round(val / equity * 100, 2) if equity else 0.0,
                    "cost": round(h["cost"] / h["shares"], 3),
                    "pnl": round(val - h["cost"], 2),
                })

        # 收盘后生成下一交易日调仓目标
        if i < len(dates) - 1 and i % max(1, rebalance_days) == 0:
            signal_marks.append(i)
            ds = date.strftime("%Y-%m-%d")
            fund = None
            if fund_data:
                fund = {"lhb": (fund_data.get("lhb") or {}).get(ds, {}),
                        "north": (fund_data.get("north") or {}).get(ds, {})}
                if seat_maps:
                    fund["seat"] = seat_maps.get(ds, {})
            picks, universe_n = _score_universe_at(
                date, data, quotes, weights, neutralize=neutralize, fund=fund)
            chosen, industry_count = [], {}
            for p in picks:
                if p["total"] < min_score:
                    continue
                ind = p["industry"]
                if industry_count.get(ind, 0) >= industry_cap:
                    continue
                chosen.append(p)
                industry_count[ind] = industry_count.get(ind, 0) + 1
                if len(chosen) >= top_n:
                    break
            pending_target = chosen
            log(f"{date:%Y-%m-%d} 调仓信号：候选 {universe_n} 只，"
                f"入选 {len(chosen)} 只")

    # 期末未平仓
    for code, h in holdings.items():
        px = last_px.get(code) or (h["cost"] / h["shares"])
        trades.append({
            "date": dates[-1].strftime("%Y-%m-%d"), "code": code,
            "name": quotes.get(code, {}).get("name", ""),
            "side": "持仓（未平）", "price": round(px, 3),
            "shares": h["shares"], "amount": round(h["shares"] * px, 2),
            "position_pct": None, "pnl": round(h["shares"] * px - h["cost"], 2),
            "return_pct": round((h["shares"] * px - h["cost"]) /
                                h["cost"] * 100, 2) if h["cost"] else 0.0,
            "buy_date": h["buy_date"].strftime("%Y-%m-%d"),
            "buy_price": round(h["cost"] / h["shares"], 3),
            "reason": "回测结束仍持有（按最新收盘价估值）", "fees": 0.0,
        })

    curve_df = pd.DataFrame(curve)
    closed = [t["pnl"] for t in trades if t["side"] == "卖出"]
    metrics = _metrics(curve_df, init_cash, closed, fees_total)

    # 推算下一次调仓：信号在 last_signal + rebalance_days 日收盘生成，
    # 再下一个交易日开盘执行；超出样本区间时用工作日序列外推（近似）。
    next_signal_date, next_exec_date = "", ""
    if signal_marks:
        nxt = signal_marks[-1] + max(1, rebalance_days)
        if nxt < len(dates):
            next_signal_date = dates[nxt].strftime("%Y-%m-%d")
        else:
            extra = pd.bdate_range(dates[-1], periods=nxt - len(dates) + 2)
            next_signal_date = extra[-1].strftime("%Y-%m-%d")
        try:
            exec_d = pd.bdate_range(pd.Timestamp(next_signal_date), periods=2)[-1]
            next_exec_date = exec_d.strftime("%Y-%m-%d")
        except Exception:
            next_exec_date = ""
    return {
        "mode": "universe", "mode_name": "全市场·多因子组合",
        "strategy_id": strategy_id, "strategy_name": strat["name"],
        "strategy_org": strat["org"], "start": curve[0]["date"],
        "end": curve[-1]["date"], "init_cash": init_cash, "months": months,
        "top_n": top_n, "pool_size": pool_size,
        "rebalance_days": rebalance_days,
        "next_signal_date": next_signal_date,
        "next_exec_date": next_exec_date,
        "universe_total": len(quotes), "pool_selected": len(pool),
        "loaded": len(data), "industry_cap": industry_cap,
        "neutralize": neutralize, "exec_constraints": exec_constraints,
        "fund_boost": bool(fund_data), "fund_weight": FUND_WEIGHT,
        "exec_stats": exec_stats,
        "curve": curve, "daily": daily, "trades": trades,
        "positions": positions,
        "metrics": metrics, "bench_label": "全市场等权",
        "factor_meta": {k: round(v, 1) for k, v in weights.items()},
        "disclaimer": "免费数据源限制下，股票池为全市场筛选后的流动性前 N 只；"
                      "历史新闻不可回溯，信息面用行业动量与量价代理。",
    }


def preview_next_picks(strategy_id: str = "high_flyer", top_n: int = 5,
                       pool_size: int = 50, max_price: Optional[float] = None,
                       exclude_star_bse: bool = False,
                       progress: Optional[Callable[[str], None]] = None) -> dict:
    """用最新行情重算下一次调仓的预选名单。

    与正式调仓同一套打分（_score_universe_at，取最新交易日截面），
    结果只用于展示与预警，不会真正下单；随行情刷新而变化。
    """
    from data_sources import fetch_all_quotes
    strat = SL.get_strategy(strategy_id)
    industry_cap = int(strat["params"].get("industry_cap", 3))
    min_score = float(strat["params"].get("min_score", 40))
    # 只预览、不写缓存：避免刷新预选把行情快照覆盖成盘中数据，
    # 导致下一次回测的股票池/成交额与上次不同、收益曲线跳变
    quotes = fetch_all_quotes(save_cache=False)
    if not quotes:
        return {"picks": [], "as_of": "", "error": "实时行情不可用"}
    pool = _load_pool(quotes, pool_size, max_price, exclude_star_bse)
    end = _today()
    start = end - pd.Timedelta(days=400)
    data = _fetch_pool_daily(pool, start, end, progress,
                             threading.Event())
    picks, universe_n = _score_universe_at(end, data, quotes, strat["weights"])
    as_of = ""
    if data:
        as_of = max(h.index[-1] for h in data.values()).strftime("%Y-%m-%d")
    # 与正式调仓相同：分数门槛 + 行业分散上限
    chosen, industry_count = [], {}
    for p in picks:
        if p["total"] < min_score:
            continue
        ind = p["industry"]
        if industry_count.get(ind, 0) >= industry_cap:
            continue
        chosen.append(p)
        industry_count[ind] = industry_count.get(ind, 0) + 1
        if len(chosen) >= top_n:
            break
    return {
        "as_of": as_of,
        "pool_selected": len(pool), "universe": universe_n,
        "picks": [
            {"rank": i + 1, "code": p["code"], "name": p["name"],
             "industry": p.get("industry", ""), "total": round(p["total"], 3)}
            for i, p in enumerate(chosen)
        ],
    }


# ============================================================
#  绩效指标
# ============================================================
def _metrics(curve: pd.DataFrame, init_cash: float, closed_pnls: list,
             fees: float, rf: float = RISK_FREE) -> dict:
    nav = curve["nav"].astype(float)
    bench = curve["bench"].astype(float)
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
    bench_return = float(bench.iloc[-1] / bench.iloc[0] - 1) \
        if bench.iloc[0] else 0.0
    days = max(1, len(nav))
    annual = float((1 + total_return) ** (252 / days) - 1) \
        if total_return > -1 else -1.0
    dd = float((nav / nav.cummax() - 1).min())
    ret = nav.pct_change().dropna()
    std = (ret - rf / 252).std()
    sharpe = float((ret.mean() - rf / 252) / std * np.sqrt(252)) \
        if std and std > 1e-9 else float("nan")
    wins = [p for p in closed_pnls if p > 0]
    losses = [p for p in closed_pnls if p <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    pf = gp / gl if gl else (float("inf") if gp else float("nan"))

    # ---------- 风险分析 ----------
    vol_ann = float(ret.std() * np.sqrt(252)) if len(ret) > 2 else 0.0
    downside = ret[ret < 0]
    down_std = float(downside.std() * np.sqrt(252)) \
        if len(downside) > 2 else 0.0
    sortino = float((ret.mean() * 252 - rf) / down_std) \
        if down_std > 1e-9 else None
    calmar = float(annual / abs(dd)) if dd < -1e-9 else None
    var95 = float(-ret.quantile(0.05) * 100) if len(ret) >= 20 else None
    pos_series = curve["position_pct"].astype(float) \
        if "position_pct" in curve.columns else pd.Series(dtype=float)
    avg_pos = float(pos_series.mean()) if len(pos_series) else 0.0
    turnover = float(pos_series.diff().abs().sum()) if len(pos_series) else 0.0

    # ---------- 稳健性检验：月度分布 + 前后半段 ----------
    monthly_win = best_month = worst_month = None
    first_half = second_half = None
    dts = pd.to_datetime(curve["date"], errors="coerce") \
        if "date" in curve.columns else pd.Series(dtype="datetime64[ns]")
    if len(nav) >= 20 and len(dts) == len(nav) and dts.notna().all():
        nav_idx = pd.Series(nav.to_numpy(), index=dts)
        month_end = nav_idx.resample("ME").last()
        mret = month_end.pct_change()
        if len(month_end):
            mret.iloc[0] = month_end.iloc[0] / nav.iloc[0] - 1
        if len(mret) >= 2:
            monthly_win = round(float((mret > 0).mean() * 100), 1)
            best_month = round(float(mret.max() * 100), 2)
            worst_month = round(float(mret.min() * 100), 2)
        mid = len(nav) // 2
        first_half = round(float(nav.iloc[mid] / nav.iloc[0] - 1) * 100, 2)
        second_half = round(float(nav.iloc[-1] / nav.iloc[mid] - 1) * 100, 2)

    return {
        "total_return": round(total_return * 100, 2),
        "bench_return": round(bench_return * 100, 2),
        "annual_return": round(annual * 100, 2),
        "excess_return": round((total_return - bench_return) * 100, 2),
        "max_drawdown": round(dd * 100, 2),
        "sharpe": None if np.isnan(sharpe) else round(sharpe, 2),
        "trades": len(closed_pnls),
        "win_rate": round(len(wins) / len(closed_pnls) * 100, 1)
        if closed_pnls else None,
        "profit_factor": None if (np.isnan(pf) or pf == float("inf"))
        else round(pf, 2),
        "fees": round(fees, 2),
        "days": days,
        "final_nav": round(float(nav.iloc[-1]), 2),
        # 风险与稳健性诊断
        "volatility": round(vol_ann * 100, 2),
        "sortino": None if sortino is None else round(sortino, 2),
        "calmar": None if calmar is None else round(calmar, 2),
        "var95": None if var95 is None else round(var95, 2),
        "monthly_win_rate": monthly_win,
        "best_month": best_month,
        "worst_month": worst_month,
        "first_half_return": first_half,
        "second_half_return": second_half,
        "avg_position": round(avg_pos, 2),
        "turnover": round(turnover, 2),
        "fees_pct": round(fees / init_cash * 100, 2) if init_cash else 0.0,
    }


if __name__ == "__main__":
    r = run_single_stock("601566", months=18, init_cash=200_000,
                         strategy_id="high_flyer",
                         progress=lambda m: print(m))
    print("\n指标:", r["metrics"])
    print("\n交易:", len(r["trades"]))
    for t in r["trades"][-5:]:
        print(t)
