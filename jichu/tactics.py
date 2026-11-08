# -*- coding: utf-8 -*-
"""网络热门战法回测（规则化近似实现）
=====================================
包含四种战法：
  1) 龙头战法：只做行业动量最强板块的龙头股，趋势不破就持有；
  2) 回头龙（龙回头）：强势股第一次回踩 MA10 缩量企稳时低吸，破位止损；
  3) 低位潜伏：60 日低位、低波动、缩量小阳时提前埋伏，等突破；
  4) 板块轮动：按行业 20 日动量轮动，每 20 个交易日换仓。

说明：战法是公开流传的规则化描述，本模块按日线价量做近似实现，
不是任何人的专有策略；收盘生成信号、次日开盘成交，不使用未来数据。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np
import pandas as pd

import backtest_service as bs

TACTICS = [
    {"id": "leader", "name": "龙头战法", "top_n": 5,
     "desc": "只在 20 日动量最强的行业内选涨幅第一的龙头，"
             "要求 MA5>MA10>MA20 多头排列、量能放大、贴近 60 日高点；"
             "跌破 MA10 或 12% 止损离场。"},
    {"id": "return_dragon", "name": "回头龙（龙回头）", "top_n": 5,
     "desc": "近 8 日内出现过 20 日涨幅超 20% 的强势股，"
             "第一次缩量回踩 MA10（±3%）且仍在 MA20 上方时低吸；"
             "跌破 MA10 下方 3%、+18% 止盈、-10% 止损或持有满 15 日离场。"},
    {"id": "ambush", "name": "低位潜伏", "top_n": 5,
     "desc": "股价处于 60 日区间低位（分位 ≤15%）、20 日波动率低于 55%、"
             "缩量并站上 MA5 时潜伏；+20% 止盈、-8% 止损或持有满 40 日离场。"},
    {"id": "sector_rotation", "name": "板块轮动", "top_n": 5,
     "desc": "每 20 个交易日按行业 20 日动量排序，取最强 3 个板块，"
             "每个板块取 2 只最强个股，站上 MA20 才买；非目标持仓在调仓日卖出。"},
]

STOP_LOSS = 0.12
STOP_LOSS_BY_TACTIC = {"leader": 0.12, "return_dragon": 0.10,
                       "ambush": 0.08, "sector_rotation": 0.12}


def list_tactics() -> list:
    return TACTICS


def get_tactic(tid: str) -> dict:
    for t in TACTICS:
        if t["id"] == tid:
            return t
    return TACTICS[0]


# ------------------------------------------------------------
#  指标矩阵
# ------------------------------------------------------------
def _matrix(data: dict, dates: pd.DatetimeIndex, field: str,
            ffill: bool = True) -> pd.DataFrame:
    df = pd.DataFrame({c: d[field] for c, d in data.items()
                       if field in d.columns}).reindex(dates)
    return df.ffill() if ffill else df


def _build_features(data: dict, dates: pd.DatetimeIndex, quotes: dict):
    close = _matrix(data, dates, "close")
    open_ = _matrix(data, dates, "open", ffill=False)
    high = _matrix(data, dates, "high")
    low = _matrix(data, dates, "low")
    vol = _matrix(data, dates, "volume")
    amt = _matrix(data, dates, "amount")

    pct = close.pct_change()
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ret5 = close / close.shift(5) - 1
    ret20 = close / close.shift(20) - 1
    vol20 = pct.rolling(20).std()
    vol_ratio = vol / vol.rolling(20).mean()
    high60 = high.rolling(60).max()
    low60 = low.rolling(60).min()
    rng = (high60 - low60).replace(0, np.nan)
    pos60 = (close - low60) / rng
    max_ret20_8 = ret20.rolling(8).max()

    industries = pd.Series({c: (quotes.get(c, {}).get("industry") or "未分类")
                            for c in close.columns})
    ind20 = ret20.T.groupby(industries).mean().T        # 日期 × 行业
    ind5 = ret5.T.groupby(industries).mean().T
    ind_rank = ind20.rank(axis=1, pct=True)

    return {"close": close, "open": open_, "vol": vol, "amt": amt,
            "pct": pct, "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "ret5": ret5, "ret20": ret20, "vol20": vol20,
            "vol_ratio": vol_ratio, "pos60": pos60,
            "max_ret20_8": max_ret20_8,
            "inds": industries, "ind20": ind20, "ind5": ind5,
            "ind_rank": ind_rank}


def _pick(row, cond: pd.Series, n: int, key: pd.Series) -> list:
    """按条件筛选并按 key 降序取前 n，返回 [(code, key值)]。"""
    ok = cond.fillna(False)
    if not ok.any():
        return []
    vals = key[ok.index][ok]
    vals = vals.dropna().sort_values(ascending=False)
    return [(c, float(vals[c])) for c in vals.index[:n]]


# ------------------------------------------------------------
#  各战法：入场信号
# ------------------------------------------------------------
def _sig_leader(f, date, held: set) -> list:
    close, ma5, ma10, ma20 = (f["close"].loc[date], f["ma5"].loc[date],
                             f["ma10"].loc[date], f["ma20"].loc[date])
    ret20, vr, pos = (f["ret20"].loc[date], f["vol_ratio"].loc[date],
                      f["pos60"].loc[date])
    rank = f["ind_rank"].loc[date]

    strong_inds = rank[rank >= 0.7].index
    ok = ((close > ma5) & (ma5 > ma10) & (ma10 > ma20)
          & (ret20 > 0.10) & (vr > 1.0) & (pos > 0.7))
    ok &= f["inds"].map(lambda x: x in strong_inds)

    picks = []
    for code in close.index:
        if code in held or not bool(ok.get(code, False)):
            continue
        picks.append((code, float(ret20.get(code, 0)),
                      f"行业{strong_inds.size}强内龙头；20日"
                      f"{ret20.get(code, 0):+.1%}，量比{vr.get(code, 0):.2f}"))
    # 每个行业只留最强一只，再按行业动量排序
    best, ind_done = [], {}
    for code, score, note in sorted(picks, key=lambda x: -x[1]):
        ind = f["inds"].get(code)
        if ind in ind_done:
            continue
        ind_done[ind] = True
        best.append((code, score, note))
    return best


def _sig_return_dragon(f, date, held: set) -> list:
    close, ma10, ma20 = (f["close"].loc[date], f["ma10"].loc[date],
                         f["ma20"].loc[date])
    ret5, vr, strong = (f["ret5"].loc[date], f["vol_ratio"].loc[date],
                        f["max_ret20_8"].loc[date])
    gap = (close / ma10 - 1).abs()
    ok = ((strong > 0.20) & (close > ma20) & (ret5 < 0.02)
          & (gap <= 0.03) & (vr < 1.0))
    picks = []
    for code, val in _pick(None, ok, len(close), f["ret20"].loc[date]):
        if code in held:
            continue
        picks.append((code, val, f"强势股回踩MA10（偏离"
                      f"{(close.get(code, 0) / ma10.get(code, 1) - 1):+.1%}），"
                      f"缩量{vr.get(code, 0):.2f}"))
    return picks


def _sig_ambush(f, date, held: set) -> list:
    close, ma5 = f["close"].loc[date], f["ma5"].loc[date]
    pos, vol20, vr = (f["pos60"].loc[date], f["vol20"].loc[date],
                      f["vol_ratio"].loc[date])
    ret20 = f["ret20"].loc[date]
    ok = ((pos <= 0.15) & (vol20 < 0.55) & (vr < 1.0)
          & (close > ma5) & (ret20 > -0.15) & (close >= 2.0))
    picks = []
    for code, val in _pick(None, ok, len(close), -ret20):
        if code in held:
            continue
        picks.append((code, val, f"60日低位分位{pos.get(code, 0):.0%}，"
                      f"缩量{vr.get(code, 0):.2f}，波动"
                      f"{vol20.get(code, 0):.0%}"))
    return picks


def _sig_rotation(f, date, held: set) -> list:
    close, ma20, ret20 = (f["close"].loc[date], f["ma20"].loc[date],
                          f["ret20"].loc[date])
    ind20 = f["ind20"].loc[date]
    top_inds = ind20.dropna().sort_values(ascending=False).index[:3]
    picks = []
    for ind in top_inds:
        sub = f["inds"][f["inds"] == ind].index
        ok = (close[sub] > ma20[sub]) & ret20[sub].notna()
        cand = ret20[sub][ok].sort_values(ascending=False).index[:2]
        for code in cand:
            if code in held:
                continue
            picks.append((code, float(ret20[code]),
                          f"板块{ind}动量第{list(top_inds).index(ind) + 1}；"
                          f"个股20日{ret20[code]:+.1%}"))
    return picks


SIGNALS = {"leader": _sig_leader, "return_dragon": _sig_return_dragon,
           "ambush": _sig_ambush, "sector_rotation": _sig_rotation}


# ------------------------------------------------------------
#  回测主循环
# ------------------------------------------------------------
def run_tactic(tactic_id: str = "leader", months: int = 18,
               init_cash: float = 200_000, top_n: Optional[int] = None,
               pool_size: int = 400, max_position: float = 0.80,
               max_price: Optional[float] = 60.0,
               progress: Optional[Callable[[str], None]] = None,
               cancel: Optional[threading.Event] = None) -> dict:
    t = get_tactic(tactic_id)
    top_n = int(top_n or t["top_n"])
    rebalance_days = 20

    def log(msg):
        if progress:
            progress(msg)

    def stopped():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("回测已取消")

    start, end = bs._range_from_months(months)
    log("正在获取全市场行情快照…")
    from data_sources import fetch_all_quotes
    quotes = fetch_all_quotes()
    pool = bs._load_pool(quotes, pool_size, max_price)
    min_pool = min(10, pool_size) if pool_size else 10
    if len(pool) < min_pool:
        raise ValueError(f"可用股票池仅 {len(pool)} 只（< {min_pool}），请检查行情源")
    stopped()
    log(f"全市场 {len(quotes)} 只 → 筛选后 {len(pool)} 只，开始加载日线…")
    data = bs._fetch_pool_daily(pool, start, end, log, cancel)
    if len(data) < min_pool:
        raise ValueError(f"仅成功加载 {len(data)} 只，样本过少，无法回测")

    all_dates = sorted({d for df in data.values() for d in df.index})
    dates = pd.DatetimeIndex([d for d in all_dates if start <= d <= end])
    if len(dates) < 30:
        raise ValueError("区间内交易日不足 30 天")

    f = _build_features(data, dates, quotes)
    close, open_ = f["close"], f["open"]
    bench_values = bs._universe_bench(close, init_cash)

    cash = float(init_cash)
    holdings = {}            # code -> {shares,cost,buy_date,days,tid}
    pending_sell = []        # [(code, reason)]
    pending_buy = []         # [(code, reason)]
    curve, daily, trades, positions = [], [], [], []
    fees_total = 0.0
    exec_stats = {"blocked_buy": 0, "blocked_sell": 0,
                  "capped_buy": 0, "impact_cost": 0.0}

    def open_price(code, date):
        try:
            v = float(open_.at[date, code])
            return v if np.isfinite(v) and v > 0 else None
        except Exception:
            return None

    def close_price(code, date):
        try:
            v = float(close.at[date, code])
            return v if np.isfinite(v) and v > 0 else None
        except Exception:
            return None

    def name_of(code):
        return quotes.get(code, {}).get("name", "")

    def sell(code, date):
        nonlocal cash, fees_total
        h = holdings.get(code)
        if not h:
            return None
        px = open_price(code, date)
        if px is None:
            exec_stats["blocked_sell"] += 1
            return None
        px *= (1 - bs.SLIPPAGE)
        gross = h["shares"] * px
        fee = gross * (bs.COMMISSION + bs.STAMP_TAX)
        cash += gross - fee
        fees_total += fee
        pnl = gross - fee - h["cost"]
        rec = {
            "date": date.strftime("%Y-%m-%d"), "code": code,
            "name": name_of(code), "side": "卖出", "price": round(px, 3),
            "shares": h["shares"], "amount": round(gross, 2),
            "position_pct": 0.0, "pnl": round(pnl, 2),
            "return_pct": round(pnl / h["cost"] * 100, 2) if h["cost"] else 0.0,
            "buy_date": h["buy_date"].strftime("%Y-%m-%d"),
            "buy_price": round(h["cost"] / h["shares"], 3),
            "reason": h.get("exit_reason", "离场"), "fees": round(fee, 2),
        }
        trades.append(rec)
        del holdings[code]
        return rec

    def buy(code, date, reason, equity):
        nonlocal cash, fees_total
        px = open_price(code, date)
        if px is None or cash <= 0:
            exec_stats["blocked_buy"] += 1
            return None
        budget = equity * max_position / max(1, top_n)
        budget = min(budget, cash * 0.98)
        px *= (1 + bs.SLIPPAGE)
        shares = int(budget / (px * (1 + bs.COMMISSION)) // 100 * 100)
        if shares <= 0:
            return None
        cost = shares * px
        fee = cost * bs.COMMISSION
        need = cost + fee
        if need > cash:
            shares = int(cash / (px * (1 + bs.COMMISSION)) // 100 * 100)
            if shares <= 0:
                return None
            cost = shares * px
            fee = cost * bs.COMMISSION
            need = cost + fee
        cash -= need
        fees_total += fee
        holdings[code] = {"shares": shares, "cost": need,
                          "buy_date": date, "days": 0}
        rec = {
            "date": date.strftime("%Y-%m-%d"), "code": code,
            "name": name_of(code), "side": "买入", "price": round(px, 3),
            "shares": shares, "amount": round(cost, 2),
            "position_pct": round(need / max(init_cash, 1) * 100, 2),
            "pnl": None, "return_pct": None, "buy_date": "", "buy_price": None,
            "reason": reason, "fees": round(fee, 2),
        }
        trades.append(rec)
        return rec

    log(f"战法「{t['name']}」开始：持仓上限 {top_n} 只，"
        f"{dates[0]:%Y-%m-%d} ~ {dates[-1]:%Y-%m-%d}")

    for i, date in enumerate(dates):
        stopped()
        executed = []
        # ---- 1) 开盘执行昨日收盘生成的委托 ----
        for code, reason in pending_sell:
            h = holdings.get(code)
            if h:
                h["exit_reason"] = reason
                r_ = sell(code, date)
                if r_:
                    executed.append(r_)
        pending_sell = []
        if pending_buy:
            equity0 = cash + sum(
                holdings[c]["shares"] * (close_price(c, dates[i - 1])
                                         or holdings[c]["cost"] / holdings[c]["shares"])
                for c in holdings)
            for code, reason in pending_buy:
                if len(holdings) >= top_n:
                    break
                r_ = buy(code, date, reason, equity0)
                if r_:
                    executed.append(r_)
            pending_buy = []

        # ---- 2) 收盘估值 ----
        equity = cash
        for code, h in holdings.items():
            px = close_price(code, date)
            if px:
                equity += h["shares"] * px
            else:
                equity += h["shares"] * h["cost"] / h["shares"]
        mv = equity - cash
        pos_pct = round(mv / equity * 100, 2) if equity else 0.0
        curve.append({"date": date.strftime("%Y-%m-%d"),
                      "nav": round(equity, 2), "bench": bench_values[i],
                      "position_pct": pos_pct})
        if executed:
            for tr in executed:
                daily.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "action": tr["side"], "code": tr["code"],
                    "name": tr["name"], "price": tr["price"],
                    "shares": tr["shares"], "cash": round(cash, 2),
                    "total": round(equity, 2),
                    "position_pct": tr.get("position_pct") or 0.0,
                    "holdings": len(holdings), "note": tr["reason"],
                    "score": None,
                })
        else:
            held = list(holdings)
            if held:
                names = "、".join(f"{c} {name_of(c)}".strip() for c in held[:8])
                if len(held) > 8:
                    names += "…"
                note = f"持有 {len(held)} 只：{names}；现金 {cash:,.0f}"
                action = "持仓"
            else:
                action, note = "空仓", "空仓等待信号"
            daily.append({"date": date.strftime("%Y-%m-%d"), "action": action,
                          "code": "", "name": "", "price": None,
                          "shares": None, "cash": round(cash, 2),
                          "total": round(equity, 2), "position_pct": pos_pct,
                          "holdings": len(holdings), "note": note,
                          "score": None})

        if executed or i == len(dates) - 1:
            for code, h in holdings.items():
                hpx = close_price(code, date) or h["cost"] / h["shares"]
                val = h["shares"] * hpx
                positions.append({
                    "date": date.strftime("%Y-%m-%d"), "code": code,
                    "name": name_of(code), "shares": h["shares"],
                    "price": round(hpx, 3), "value": round(val, 2),
                    "weight": round(val / equity * 100, 2) if equity else 0.0,
                    "cost": round(h["cost"] / h["shares"], 3),
                    "pnl": round(val - h["cost"], 2),
                })

        # ---- 3) 收盘后生成次日委托 ----
        for code, h in holdings.items():
            h["days"] = h.get("days", 0) + 1
            hpx = close_price(code, date)
            if not hpx:
                continue
            cost_px = h["cost"] / h["shares"]
            ret = hpx / cost_px - 1
            stop = STOP_LOSS_BY_TACTIC.get(tactic_id, STOP_LOSS)
            reason = None
            if ret <= -stop:
                reason = f"止损：回撤超过 {stop:.0%}"
            elif tactic_id == "leader":
                ma10 = f["ma10"].at[date, code]
                if np.isfinite(ma10) and hpx < ma10:
                    reason = "跌破MA10，龙头趋势结束"
            elif tactic_id == "return_dragon":
                ma10 = f["ma10"].at[date, code]
                if np.isfinite(ma10) and hpx < ma10 * 0.97:
                    reason = "跌破回踩平台（MA10下方3%），低吸失败"
                elif ret >= 0.18:
                    reason = "止盈：浮盈达到 +18%"
                elif h["days"] >= 15:
                    reason = "持有满15个交易日离场"
            elif tactic_id == "ambush":
                if ret >= 0.20:
                    reason = "止盈：浮盈达到 +20%"
                elif h["days"] >= 40:
                    reason = "持有满40个交易日离场"
            if reason and code not in {c for c, _ in pending_sell}:
                pending_sell.append((code, reason))

        if i >= len(dates) - 1:
            break

        slots = top_n - len(holdings) - len(pending_buy)
        selling = len(pending_sell)
        if slots + selling <= 0:
            continue
        if tactic_id == "sector_rotation":
            if i % rebalance_days != 0:
                continue
            target = SIGNALS["sector_rotation"](f, date, set(holdings))
            target_codes = {c for c, _, _ in target}
            for code in list(holdings):
                if code not in target_codes and \
                        code not in {c for c, _ in pending_sell}:
                    pending_sell.append((code, "调仓：未进入本期轮动目标"))
            for code, _, note in target[:top_n]:
                if code not in holdings and code not in {c for c, _ in pending_buy}:
                    pending_buy.append((code, note))
        else:
            held_all = set(holdings) | {c for c, _ in pending_buy}
            picks = SIGNALS[tactic_id](f, date, held_all)
            for code, _, note in picks[:max(0, slots + selling)]:
                pending_buy.append((code, note))

    # 期末未平仓
    for code, h in holdings.items():
        px = close_price(code, dates[-1]) or h["cost"] / h["shares"]
        trades.append({
            "date": dates[-1].strftime("%Y-%m-%d"), "code": code,
            "name": name_of(code), "side": "持仓（未平）",
            "price": round(px, 3), "shares": h["shares"],
            "amount": round(h["shares"] * px, 2), "position_pct": None,
            "pnl": round(h["shares"] * px - h["cost"], 2),
            "return_pct": round((h["shares"] * px - h["cost"]) /
                                h["cost"] * 100, 2) if h["cost"] else 0.0,
            "buy_date": h["buy_date"].strftime("%Y-%m-%d"),
            "buy_price": round(h["cost"] / h["shares"], 3),
            "reason": "回测结束仍持有（按最新收盘价估值）", "fees": 0.0,
        })

    curve_df = pd.DataFrame(curve)
    closed = [x["pnl"] for x in trades if x["side"] == "卖出"]
    metrics = bs._metrics(curve_df, init_cash, closed, fees_total)
    return {
        "mode": "tactic", "mode_name": f"战法·{t['name']}",
        "strategy_id": f"tactic_{tactic_id}", "strategy_name": t["name"],
        "strategy_org": "网络热门战法（规则化近似）",
        "tactic_id": tactic_id, "tactic_desc": t["desc"],
        "start": curve[0]["date"], "end": curve[-1]["date"],
        "init_cash": init_cash, "months": months, "top_n": top_n,
        "pool_size": pool_size, "rebalance_days": rebalance_days,
        "max_position": max_position, "max_price": max_price,
        "universe_total": len(quotes), "pool_selected": len(pool),
        "loaded": len(data), "exec_constraints": True, "exec_stats": exec_stats,
        "fund_boost": False, "fund_weight": 0.0,
        "curve": curve, "daily": daily, "trades": trades,
        "positions": positions, "metrics": metrics,
        "bench_label": "全市场等权",
        "disclaimer": "战法为公开流传规则的规则化近似实现，仅用日线价量，"
                      "不代表任何机构或个人真实策略。",
    }
