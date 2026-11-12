# -*- coding: utf-8 -*-
"""开盘前荐股专用的幻方量化近似模式。

只读复用回测的全市场横截面评分，不执行回测、调仓或交易；结果适配荐股页。
幻方真实模型与参数未公开，本模块仅为公开方法论的本地近似实现。
"""
import datetime as dt
import threading
from typing import Callable, Optional

import pandas as pd

import backtest_service as bt
import market_info
import strategy_library as SL
from data_sources import fetch_all_quotes, is_trading_time, market_of_code
from market_info import load_latest_snapshot


def _board_of(code: str) -> str:
    code = str(code)
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("4", "8", "9")):
        return "北交所"
    return ""


def _to_pick(row: dict, quote: dict, rank: int, session: str,
             display_only: bool = False) -> dict:
    code = str(row["code"])
    factors = row.get("factors") or {}
    risk_item = factors.get("risk") or {}
    risk_max = float(risk_item.get("max") or 1)
    risk_ratio = float(risk_item.get("score") or 0) / risk_max if risk_max else 0
    risk = "低" if risk_ratio >= 0.7 else ("中" if risk_ratio >= 0.4 else "高")
    board = _board_of(code)
    reasons = ["核心因子贡献：" + row["reason"]] if row.get("reason") else []
    if risk_item.get("note"):
        reasons.append("风险质量：" + risk_item["note"])
    if display_only:
        reasons.append(f"{board}仅作模型评分展示，不占主板/创业板推荐席位")
    tags = ["幻方横截面"]
    if display_only:
        tags.append(f"{board}·仅展示")
    return {
        "rank": rank,
        "code": code,
        "name": row.get("name") or quote.get("name", ""),
        "market": market_of_code(code),
        "price": float(quote.get("price") or 0),
        "change_pct": float(quote.get("change_pct") or 0),
        "score": round(float(row.get("total") or 0), 1),
        "industry": row.get("industry") or quote.get("industry") or "未分类",
        "turnover": float(quote.get("turnover") or 0),
        "amount": float(quote.get("amount") or 0),
        "total_mv": float(quote.get("total_mv") or quote.get("circ_mv") or 0),
        "hit_concepts": [],
        "leader_flag": "",
        "risk": risk,
        "reasons": reasons,
        "factors": factors,
        "factor_scores": {k: v.get("score", 0) for k, v in factors.items()},
        "tags": tags,
        "data_quality": "完整",
        "pe": float(quote.get("pe") or 0),
        "pb": float(quote.get("pb") or 0),
        "chg60": float(quote.get("chg60") or 0),
        "chg_ytd": float(quote.get("chg_ytd") or 0),
        "rotation_score": (factors.get("rotation") or {}).get("score", 0),
        "leader_score": (factors.get("leader") or {}).get("score", 0),
        "hot_score": (factors.get("theme") or {}).get("score", 0),
        "news_score": 0,
        "tech_score": (factors.get("trend") or {}).get("score", 0),
        "session_bonus": 0,
        "session_note": "午盘批使用上午最新行情重新排名，不叠加原荐股午盘修正" \
            if session == "noon" else "",
        "morning": {},
        "ret20": float(row.get("ret20") or 0),
        "display_only": display_only,
        "board": board,
    }


def recommend(snapshot=None, session: str = "morning",
              top_n: Optional[int] = None, pool_size: Optional[int] = None,
              industry_cap: Optional[int] = None,
              min_score: Optional[float] = None,
              exclude_star_bse: bool = True,
              progress: Optional[Callable[[str], None]] = None) -> dict:
    """用幻方策略权重进行最新横截面荐股，不修改或执行量化回测。"""
    t0 = dt.datetime.now()
    notes = ["幻方官方未公开模型与参数，本结果为本程序公开方法论近似"]
    degraded = False

    def log(message):
        if progress:
            progress(message)

    strat = SL.get_strategy("high_flyer")
    params = strat.get("params") or {}
    top_n = int(top_n or params.get("top_n", 20))
    pool_size = int(pool_size or params.get("max_candidates", 150))
    industry_cap = int(industry_cap or params.get("industry_cap", 5))
    min_score = float(min_score if min_score is not None
                      else params.get("min_score", 35))
    weights = SL.validate_weights(strat["weights"])

    if snapshot is None:
        snapshot = load_latest_snapshot()
        if snapshot is not None:
            notes.append("市场快照来自本地缓存")
            degraded = True
    if snapshot is None:
        snapshot = market_info.MarketSnapshot(
            asof=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_date=market_info.recent_trade_dates(1)[0])
        notes.append("无市场快照：仅使用行情与日线横截面评分")
        degraded = True

    log("幻方模式：获取全市场最新行情（不写入回测行情缓存）…")
    try:
        quotes = fetch_all_quotes(force=is_trading_time(), save_cache=False)
    except Exception as exc:
        notes.append(f"实时行情失败：{exc}")
        try:
            quotes = fetch_all_quotes(force=False, save_cache=False)
            notes.append("已降级使用本地缓存行情")
        except Exception as cache_exc:
            quotes = {}
            notes.append(f"缓存行情也不可用：{cache_exc}")
        degraded = True
    if not is_trading_time():
        notes.append("非交易时段：行情为最近一个交易日收盘数据")
    if session == "noon":
        notes.append("午盘批：使用上午最新行情重排，不叠加原荐股午盘修正")

    pool = bt._load_pool(quotes, pool_size, None, exclude_star_bse)
    log(f"幻方模式：流动性候选池 {len(pool)} 只，加载日线…")
    end = pd.Timestamp(dt.date.today())
    start = end - pd.Timedelta(days=400)
    data = bt._fetch_pool_daily(pool, start, end, log, threading.Event())
    scored, scored_count = bt._score_universe_at(
        end, data, quotes, weights)
    if len(data) < len(pool):
        notes.append(f"候选池 {len(pool) - len(data)} 只日线不足或获取失败，已跳过")
    if pool and not data:
        degraded = True
        notes.append("候选股日线均不可用，本次无法生成有效横截面评分")

    chosen, industry_count = [], {}
    for row in scored:
        if row["total"] < min_score:
            continue
        industry = row.get("industry") or "未分类"
        if industry_count.get(industry, 0) >= industry_cap:
            continue
        chosen.append(row)
        industry_count[industry] = industry_count.get(industry, 0) + 1
        if len(chosen) >= top_n:
            break

    # 与回测预选一致：科创板/北交所单独评分展示，不占正式推荐席位。
    display_scored, star_pool = [], []
    if exclude_star_bse and quotes:
        all_pool = bt._load_pool(quotes, 0, None, False)
        star_pool = [q for q in all_pool if _board_of(q.get("code"))][:12]
        if star_pool:
            log(f"补充计算科创板/北交所展示标的 {len(star_pool)} 只…")
            star_data = bt._fetch_pool_daily(
                star_pool, start, end, log, threading.Event())
            display_scored, _ = bt._score_universe_at(
                end, star_data, quotes, weights)

    picks = [_to_pick(row, quotes.get(row["code"], {}), i, session)
             for i, row in enumerate(chosen, 1)]
    shown = 0
    for row in display_scored:
        if row["total"] < min_score:
            continue
        shown += 1
        picks.append(_to_pick(row, quotes.get(row["code"], {}),
                              len(picks) + 1, session, display_only=True))
        if shown >= 3:
            break

    ladder = sorted(snapshot.limit_up or [],
                    key=lambda s: (s.get("boards", 0),
                                   s.get("seal_fund", 0)), reverse=True)
    ladder_out = [{
        "code": s.get("code"), "name": s.get("name"),
        "market": market_of_code(str(s.get("code") or "")),
        "price": s.get("price"), "boards": s.get("boards", 0),
        "reasons": s.get("reasons", []),
        "break_count": s.get("break_count", 0),
        "seal_fund": round((s.get("seal_fund", 0) or 0) / 1e8, 2),
    } for s in ladder]
    as_of = ""
    if data:
        as_of = max(h.index[-1] for h in data.values()).strftime("%Y-%m-%d")
    elapsed = (dt.datetime.now() - t0).total_seconds()
    session_brief = (
        "幻方量化午盘批：上午最新行情 + 最近日线横截面排名，不使用原荐股午盘加分；"
        if session == "noon" else
        "幻方量化模式：全市场流动性候选池 + 日线横截面多因子排名 + 行业分散；"
    ) + "公开方法论近似，非幻方官方模型"
    log(f"幻方荐股完成：正式 {len(chosen)} 只，展示 {shown} 只，"
        f"耗时 {elapsed:.1f}s")
    return {
        "model": "high_flyer",
        "strategy_id": "high_flyer",
        "strategy_name": strat["name"],
        "strategy_org": strat["org"],
        "asof": snapshot.asof,
        "trade_date": as_of or snapshot.trade_date,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "picks": picks,
        "ladder": ladder_out,
        "rotation": list(snapshot.rotation or [])[:12],
        "degraded": degraded,
        "data_notes": notes,
        "session": session,
        "session_brief": session_brief,
        "universe": len(quotes),
        "candidates": len(pool) + len(star_pool),
        "scored_count": scored_count,
        "slots": len(chosen),
        "display_count": shown,
        "factors_meta": {k: {"name": SL.FACTOR_NAME[k], "max": weights[k]}
                         for k in SL.FACTOR_KEYS},
        "elapsed": round(elapsed, 1),
        "disclaimer": "幻方真实模型未公开；结果仅为本地近似量化评分，不构成投资建议。",
    }
