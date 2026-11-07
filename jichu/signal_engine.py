# -*- coding: utf-8 -*-
"""
个股自选 · 买卖信号引擎
=======================
对应「个股自选」模块，为自选股提供买入/卖出信号提示。

基于日线多指标多空打分（信号在收盘后形成、次日执行，避免未来函数）：
  均线（MA5/10/20/60 排列与金叉）、MACD（金叉/零轴/红绿柱）、
  RSI（超买超卖）、布林带（触轨）、量能（放量方向）、动量。

每个指标给出 -2 ~ +2 的多空分与文字理由，加权合成总分，映射到：
  强烈买入 / 买入 / 观望 / 卖出 / 强烈卖出，并给出仓位建议。

仅为技术信号整理，不构成投资建议。
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd


# ============================================================
#  指标计算
# ============================================================
def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> dict:
    """在日线（含 close/high/low/volume）上计算全部指标，返回最新一日快照。"""
    close = df["close"]
    n = len(df)

    ma = {str(p): round(float(close.rolling(p).mean().iloc[-1]), 3)
          for p in (5, 10, 20, 60)}
    # 均线金叉（MA5 与 MA20）：昨日 MA5<=MA20，今日 MA5>MA20
    ma5, ma20 = close.rolling(5).mean(), close.rolling(20).mean()
    golden = bool(ma5.iloc[-2] <= ma20.iloc[-2] and ma5.iloc[-1] > ma20.iloc[-1])
    dead = bool(ma5.iloc[-2] >= ma20.iloc[-2] and ma5.iloc[-1] < ma20.iloc[-1])

    # MACD
    dif = _ema(close, 12) - _ema(close, 26)
    dea = _ema(dif, 9)
    macd_hist = (dif - dea) * 2
    dif_now, dea_now = float(dif.iloc[-1]), float(dea.iloc[-1])
    hist_now, hist_prev = float(macd_hist.iloc[-1]), float(macd_hist.iloc[-2])
    macd_golden = bool(dif.iloc[-2] <= dea.iloc[-2] and dif_now > dea_now)
    macd_dead = bool(dif.iloc[-2] >= dea.iloc[-2] and dif_now < dea_now)

    # RSI14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    if np.isnan(rsi):
        rsi = 50.0

    # 布林带（20, 2σ）
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    boll_up = float((mid + 2 * std).iloc[-1])
    boll_dn = float((mid - 2 * std).iloc[-1])

    # 量比（当日量 / 近5日均量）
    vol = df["volume"]
    vol_ratio = float(vol.iloc[-1] / vol.rolling(5).mean().iloc[-2]) \
        if vol.rolling(5).mean().iloc[-2] else 1.0

    # 动量
    price = float(close.iloc[-1])
    ret5 = float(close.iloc[-1] / close.iloc[-6] - 1) if n > 6 else 0.0
    ret20 = float(close.iloc[-1] / close.iloc[-21] - 1) if n > 21 else 0.0

    return {
        "price": round(price, 3), "ma": ma,
        "golden": golden, "dead": dead,
        "dif": round(dif_now, 3), "dea": round(dea_now, 3),
        "hist": round(hist_now, 3), "hist_growing": hist_now > hist_prev,
        "macd_golden": macd_golden, "macd_dead": macd_dead,
        "rsi": round(rsi, 1),
        "boll_up": round(boll_up, 3), "boll_dn": round(boll_dn, 3),
        "vol_ratio": round(vol_ratio, 2),
        "ret5": round(ret5 * 100, 2), "ret20": round(ret20 * 100, 2),
        "above_ma20": price > ma["20"],
        "bull_align": ma["5"] > ma["20"] > ma["60"],
    }


# ============================================================
#  多空打分
# ============================================================
def score_signals(ind: dict) -> tuple:
    """返回 (总分, 多头理由[], 空头理由[])。"""
    bull, bear = [], []
    total = 0.0

    # 1. 均线（权重高）
    ma = ind["ma"]
    if ind["bull_align"]:
        total += 2; bull.append(f"均线多头排列（MA5 {ma['5']} > MA20 {ma['20']} > MA60 {ma['60']}）")
    if ind["golden"]:
        total += 1.5; bull.append("MA5 上穿 MA20 形成金叉")
    if ind["dead"]:
        total -= 1.5; bear.append("MA5 下穿 MA20 形成死叉")
    if ind["above_ma20"]:
        total += 1; bull.append(f"股价站在 MA20({ma['20']}) 上方")
    else:
        total -= 1; bear.append(f"股价跌破 MA20({ma['20']})")

    # 2. MACD
    if ind["dif"] > ind["dea"]:
        total += 1; bull.append("MACD 金叉状态（DIF 在 DEA 上方）")
    else:
        total -= 1; bear.append("MACD 死叉状态（DIF 在 DEA 下方）")
    if ind["dif"] > 0:
        total += 0.5; bull.append("DIF 在零轴上方，中期趋势偏多")
    else:
        total -= 0.5; bear.append("DIF 在零轴下方，中期趋势偏空")
    if ind["hist"] > 0 and ind["hist_growing"]:
        total += 0.5; bull.append("红柱放大，多头动能增强")
    elif ind["hist"] < 0 and not ind["hist_growing"]:
        total -= 0.5; bear.append("绿柱放大，空头动能增强")

    # 3. RSI
    rsi = ind["rsi"]
    if rsi < 30:
        total += 1.5; bull.append(f"RSI={rsi} 超卖，存在反弹需求")
    elif rsi > 70:
        total -= 1.5; bear.append(f"RSI={rsi} 超买，存在回调压力")
    elif rsi >= 55:
        total += 0.5; bull.append(f"RSI={rsi} 偏强")
    elif rsi <= 45:
        total -= 0.5; bear.append(f"RSI={rsi} 偏弱")

    # 4. 布林带
    if ind["price"] <= ind["boll_dn"]:
        total += 1; bull.append("股价触及布林下轨，短线超跌")
    elif ind["price"] >= ind["boll_up"]:
        total -= 1; bear.append("股价触及布林上轨，短线超涨")

    # 5. 量能 + 动量
    vr = ind["vol_ratio"]
    if vr > 1.5 and ind["ret5"] > 0:
        total += 1; bull.append(f"放量上涨（量比{vr}），资金进场")
    elif vr > 1.5 and ind["ret5"] < 0:
        total -= 1; bear.append(f"放量下跌（量比{vr}），资金出逃")
    if ind["ret5"] > 8:
        total -= 0.5; bear.append(f"近5日涨{ind['ret5']}%，短线涨幅过大")
    elif ind["ret20"] < -12:
        total -= 0.5; bear.append(f"近20日跌{abs(ind['ret20'])}%，趋势偏弱")

    return round(total, 2), bull, bear


def _grade(total: float) -> tuple:
    """总分 → (信号等级, 置信度)。"""
    a = abs(total)
    conf = round(min(a / 7.0, 0.99), 2)
    if total >= 4:
        return "强烈买入", conf
    if total >= 1.5:
        return "买入", conf
    if total > -1.5:
        return "观望", round(min(a / 3.0, 0.6), 2)
    if total > -4:
        return "卖出", conf
    return "强烈卖出", conf


def _position_advice(grade: str, total: float) -> str:
    if grade == "强烈买入":
        return "技术面共振做多，可考虑 5~7 成仓位，分批介入，跌破MA20止损"
    if grade == "买入":
        return "技术面偏多，可考虑 3~5 成仓位，回踩MA20附近低吸"
    if grade == "卖出":
        return "技术面转弱，建议减仓至 2~3 成，反弹减磅"
    if grade == "强烈卖出":
        return "技术面共振走弱，建议清仓避险，等待企稳信号"
    return "多空信号交织，建议观望或维持现有仓位，等待方向明确"


# ============================================================
#  数据结构与入口
# ============================================================
@dataclass
class StockSignal:
    code: str
    name: str
    price: float
    signal: str
    confidence: float
    score: float
    bull: List[str] = field(default_factory=list)
    bear: List[str] = field(default_factory=list)
    indicators: dict = field(default_factory=dict)
    advice: str = ""


def analyze_stock(code: str, name: Optional[str] = None,
                  start: Optional[str] = None) -> Optional[StockSignal]:
    """分析单只股票，返回 StockSignal；取数失败返回 None。"""
    from data_sources import fetch_daily
    end = dt.date.today().strftime("%Y%m%d")
    if start is None:
        start = (dt.date.today() - dt.timedelta(days=260)).strftime("%Y%m%d")
    try:
        df = fetch_daily(code, start, end)
        if df is None or len(df) < 30:
            return None
        ind = compute_indicators(df)
        total, bull, bear = score_signals(ind)
        grade, conf = _grade(total)
        return StockSignal(
            code=code, name=name or "", price=ind["price"],
            signal=grade, confidence=conf, score=total,
            bull=bull, bear=bear, indicators=ind,
            advice=_position_advice(grade, total))
    except Exception:
        return None


def analyze_watchlist(items: List[dict], workers: int = 8) -> List[dict]:
    """
    批量分析自选股。items: [{code,name}]，返回信号 dict 列表（按信号强度排序）。
    """
    out = []

    def work(it):
        return analyze_stock(it["code"], it.get("name"))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(work, it) for it in items]):
            s = fut.result()
            if s:
                out.append(s.__dict__)
    order = {"强烈买入": 0, "买入": 1, "观望": 2, "卖出": 3, "强烈卖出": 4}
    out.sort(key=lambda x: (order.get(x["signal"], 5), -x["score"]))
    return out


if __name__ == "__main__":
    watch = [{"code": "601566", "name": "九牧王"},
             {"code": "002708", "name": "光洋股份"},
             {"code": "600519", "name": "贵州茅台"}]
    for s in analyze_watchlist(watch):
        print(f"\n{s['code']} {s['name']} 现价{s['price']} → "
              f"{s['signal']}（置信度{s['confidence']:.0%}, 分{s['score']}）")
        for b in s["bull"]:
            print("  +", b)
        for b in s["bear"]:
            print("  -", b)
        print("  建议:", s["advice"])
