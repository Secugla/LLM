# -*- coding: utf-8 -*-
"""
量化回测服务
============
对应「量化回测」模块，为 GUI 提供结构化回测结果：
  1. 收益回测曲线：策略净值 vs 买入持有基准、回撤、每日仓位
  2. 每日操作：买入 / 卖出 / 持仓 / 空仓，含价格、数量、金额、费用
  3. 仓位大小：每日持仓市值占总资产比例
  4. 绩效指标：累计/年化收益、最大回撤、夏普、胜率、交易次数、费用

策略：双均线（MA_fast 上穿 MA_slow 次日开盘全仓买入，下穿卖出），
信号 shift(1) 防未来函数；A股费用：佣金万三双边、印花税千一卖出、滑点0.1%。
"""

from typing import Callable, Optional

import numpy as np
import pandas as pd


def _ma_signals(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    d = df.copy()
    d["ma_fast"] = d["close"].rolling(fast).mean()
    d["ma_slow"] = d["close"].rolling(slow).mean()
    d["target"] = np.where(d["ma_fast"] > d["ma_slow"], 1, 0)
    d["signal"] = d["target"].shift(1).fillna(0).astype(int)
    return d


def run_backtest(code: str, start: str, end: str,
                 fast: int = 5, slow: int = 20, init_cash: float = 100_000,
                 commission: float = 0.0003, slippage: float = 0.001,
                 stamp_tax: float = 0.001,
                 progress: Optional[Callable[[str], None]] = None) -> dict:
    """执行双均线回测，返回 GUI 友好的结构化结果。"""
    def log(m):
        if progress:
            progress(m)

    from data_sources import fetch_daily
    log(f"正在获取 {code} 日线…")
    df = fetch_daily(code, start, end)
    if df is None or len(df) < slow + 5:
        raise ValueError(f"数据不足：至少需要 {slow + 5} 个交易日")
    name = ""
    try:
        import akshare as ak
        tbl = ak.stock_info_a_code_name()
        hit = tbl[tbl["code"] == code]
        if not hit.empty:
            name = str(hit["name"].iloc[0])
    except Exception:
        pass

    df = _ma_signals(df, fast, slow)

    cash = float(init_cash)
    shares = 0
    entry_price = entry_date = None
    curve, daily, trades = [], [], []
    fees_total = 0.0

    for i in range(len(df)):
        date = df.index[i]
        o, c = float(df["open"].iloc[i]), float(df["close"].iloc[i])
        sig = int(df["signal"].iloc[i])
        action = ""
        note = ""

        if sig == 1 and shares == 0:
            buy_price = o * (1 + slippage)
            lots = int(cash / (buy_price * (1 + commission)) // 100 * 100)
            if lots > 0:
                cost = lots * buy_price
                fee = cost * commission
                cash -= cost + fee
                shares = lots
                entry_price, entry_date = buy_price, date
                fees_total += fee
                action = "买入"
                note = f"开盘{buy_price:.2f}买入{lots}股，金额{cost:,.0f}，佣金{fee:,.0f}"
            else:
                action = "空仓"
                note = "资金不足1手，未成交"
        elif sig == 0 and shares > 0:
            sell_price = o * (1 - slippage)
            revenue = shares * sell_price
            fee = revenue * (commission + stamp_tax)
            cash += revenue - fee
            fees_total += fee
            buy_cost = shares * entry_price * (1 + commission)
            pnl = revenue - fee - buy_cost
            trades.append({
                "buy_date": entry_date.strftime("%Y-%m-%d"),
                "buy_price": round(entry_price, 3),
                "sell_date": date.strftime("%Y-%m-%d"),
                "sell_price": round(sell_price, 3),
                "shares": shares,
                "hold_days": (date - entry_date).days,
                "pnl": round(pnl, 2),
                "return_pct": round(pnl / buy_cost * 100, 2),
            })
            action = "卖出"
            note = (f"开盘{sell_price:.2f}卖出{shares}股，金额{revenue:,.0f}，"
                    f"税费{fee:,.0f}，盈亏{pnl:,.0f}")
            shares = 0
            entry_price = entry_date = None
        else:
            action = "持仓" if shares > 0 else "空仓"

        total = cash + shares * c
        mv = shares * c
        pos_pct = round(mv / total * 100, 2) if total else 0.0

        curve.append({
            "date": date.strftime("%Y-%m-%d"),
            "nav": round(total, 2),
            "position_pct": pos_pct,
            "close": round(c, 3),
        })
        daily.append({
            "date": date.strftime("%Y-%m-%d"),
            "action": action,
            "price": round(c, 3),
            "shares": shares,
            "cash": round(cash, 2),
            "total": round(total, 2),
            "position_pct": pos_pct,
            "note": note,
        })

    # 基准：买入持有
    first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    bench_nav = [round(init_cash * (float(df["close"].iloc[i]) / first), 2)
                 for i in range(len(df))]
    for i, row in enumerate(curve):
        row["bench"] = bench_nav[i]

    # 未平仓
    if shares > 0 and entry_date is not None:
        trades.append({
            "buy_date": entry_date.strftime("%Y-%m-%d"),
            "buy_price": round(entry_price, 3),
            "sell_date": df.index[-1].strftime("%Y-%m-%d"),
            "sell_price": round(last, 3), "shares": shares,
            "hold_days": (df.index[-1] - entry_date).days,
            "pnl": round(shares * (last - entry_price), 2),
            "return_pct": round((last / entry_price - 1) * 100, 2),
            "open": True,
        })

    metrics = _metrics(pd.DataFrame(curve), trades, init_cash, fees_total)
    return {
        "code": code, "name": name,
        "start": df.index[0].strftime("%Y-%m-%d"),
        "end": df.index[-1].strftime("%Y-%m-%d"),
        "fast": fast, "slow": slow, "init_cash": init_cash,
        "curve": curve, "daily": daily, "trades": trades,
        "metrics": metrics,
    }


def _metrics(curve: pd.DataFrame, trades: list, init_cash: float,
             fees: float, rf: float = 0.02) -> dict:
    nav = curve["nav"]
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
    bench_return = float(curve["close"].iloc[-1] / curve["close"].iloc[0] - 1)
    days = len(nav)
    annual = float((1 + total_return) ** (252 / days) - 1) \
        if total_return > -1 else -1.0
    dd = float((nav / nav.cummax() - 1).min())
    ret = nav.pct_change().dropna()
    std = (ret - rf / 252).std()
    sharpe = float((ret.mean() - rf / 252) / std * np.sqrt(252)) \
        if std and std > 0 else float("nan")
    closed = [t for t in trades if not t.get("open")]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl else (float("inf") if gp else float("nan"))
    return {
        "total_return": round(total_return * 100, 2),
        "bench_return": round(bench_return * 100, 2),
        "annual_return": round(annual * 100, 2),
        "excess_return": round((total_return - bench_return) * 100, 2),
        "max_drawdown": round(dd * 100, 2),
        "sharpe": None if np.isnan(sharpe) else round(sharpe, 2),
        "trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": None if (np.isnan(pf) or pf == float("inf")) else round(pf, 2),
        "fees": round(fees, 2),
        "days": days,
    }


if __name__ == "__main__":
    r = run_backtest("601566", "20240101", "20261029",
                     init_cash=50_000, progress=lambda m: print(m))
    print("\n指标:", r["metrics"])
    print("\n最近10日操作:")
    for d in r["daily"][-10:]:
        print(f"  {d['date']} {d['action']} 仓位{d['position_pct']}% "
              f"总{d['total']:,.0f} {d['note']}")
