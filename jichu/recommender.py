# -*- coding: utf-8 -*-
"""
开盘前多因子选股引擎（第四功能模块）
====================================
参考主流量化多因子框架（如幻方量化的基本思想：多因子打分、流动性过滤、
动量趋势、量价配合、板块轮动、龙头强度、风险剔除），把原来的简单规则
升级为「可解释的百分制多因子模型」。每只标的都会输出分数构成与命中理由。

因子体系（合计 100 分）
  F1 趋势动量 15   20日/5日涨幅、均线多头、周线多头（日线数据缺失时按快照降级）
  F2 量价配合 12   量比、换手率、当日涨跌与量能是否同步
  F3 流动性   8    成交额、换手区间、流通市值（过滤僵尸股与过度投机）
  F4 板块轮动 20   行业涨幅、今日涨停家数、近5日涨停动量、轮动阶段
  F5 龙头强度 15   连板高度、封板资金、板块龙头/次龙头、板块内涨停共振
  F6 题材热度 12   涨停股概念聚类频次、利好新闻概念
  F7 资金强度 8    成交额绝对水平、涨幅位置、日线突破
  F8 风险质量 10   高位、超买、破位、异常换手、波动率扣分（ST/停牌/新股直接剔除）

数据与稳定性：
  · 行情：东财全市场清单（换手/市值/行业），失败自动降级腾讯/本地缓存；
  · 技术：日线磁盘缓存 + 新浪→腾讯→东财多通道，非交易时段使用最近收盘；
  · 题材：东财 F10，7 天磁盘缓存；
  · 快照缺失/接口失败时自动降级为「行情快照 + 技术面」模式并在结果中标注，
    保证开盘前（含非交易时段）仍能稳定产出名单。

注意：结果仅为量化模型整理，不构成投资建议。
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import market_info
from data_sources import (fetch_all_quotes, fetch_daily_fast, is_trading_time,
                          market_of_code)
from market_info import fetch_core_themes, load_latest_snapshot
from picker_engine import _weekly_features, daily_features

# 单只股票最大允许涨幅（按板块）
def limit_pct_of(code: str) -> float:
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def market_of(code: str) -> str:
    return market_of_code(code)


# ============================================================
#  因子定义与结果结构
# ============================================================
FACTOR_META = {
    "trend": {"name": "趋势动量", "max": 15},
    "volume": {"name": "量价配合", "max": 12},
    "liquidity": {"name": "流动性", "max": 8},
    "rotation": {"name": "板块轮动", "max": 20},
    "leader": {"name": "龙头强度", "max": 15},
    "theme": {"name": "题材热度", "max": 12},
    "capital": {"name": "资金强度", "max": 8},
    "risk": {"name": "风险质量", "max": 10},
}


@dataclass
class Pick:
    rank: int
    code: str
    name: str
    market: str
    price: float
    change_pct: float
    score: float
    industry: str = ""
    turnover: float = 0.0
    amount: float = 0.0
    total_mv: float = 0.0
    hit_concepts: List[str] = field(default_factory=list)
    leader_flag: str = ""
    risk: str = ""
    reasons: List[str] = field(default_factory=list)
    factors: dict = field(default_factory=dict)       # 因子分/上限/说明
    factor_scores: dict = field(default_factory=dict)  # 因子简表（界面列）
    tags: List[str] = field(default_factory=list)
    data_quality: str = "完整"
    pe: float = 0.0
    pb: float = 0.0
    chg60: float = 0.0
    chg_ytd: float = 0.0
    # 兼容旧界面字段
    rotation_score: float = 0.0
    leader_score: float = 0.0
    hot_score: float = 0.0
    news_score: float = 0.0
    tech_score: float = 0.0
    # 午盘批：上午盘面与午后修正
    session_bonus: float = 0.0
    session_note: str = ""
    morning: dict = field(default_factory=dict)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ============================================================
#  信息面特征（来自市场快照）
# ============================================================
def _build_info_features(snapshot) -> dict:
    concept_weight = {}
    max_cw = 1
    news_concept = {}
    max_news = 1
    sector_pct = {}
    rotation = {}
    limit_map = {}
    if snapshot is not None:
        concept_weight = {h.get("concept", ""): h.get("count", 0)
                          for h in (snapshot.hot_concepts or [])}
        max_cw = max(concept_weight.values(), default=1)
        for n in (snapshot.news or []):
            if (n.get("sentiment", 0) or 0) > 0.25:
                for c in (n.get("concepts") or []):
                    news_concept[c] = news_concept.get(c, 0) + n["sentiment"]
        max_news = max(news_concept.values(), default=1)
        sector_pct = {s.get("name", ""): s.get("change_pct", 0)
                      for s in (snapshot.sectors or [])}
        try:
            rotation = market_info.fetch_sector_rotation(snapshot.limit_up)
        except Exception:
            rotation = {}
        for s in (snapshot.limit_up or []):
            limit_map[s.get("code")] = s

    # 板块龙头：按 连板数 → 封单额 → 首封时间 排序
    groups = {}
    for s in (snapshot.limit_up if snapshot else []) or []:
        ind = (s.get("industry") or "").strip()
        if ind:
            groups.setdefault(ind, []).append(s)
    leader_map = {}
    for ind, lst in groups.items():
        ranked = sorted(lst, key=lambda s: (
            s.get("boards", 0), s.get("seal_fund", 0),
            -int(str(s.get("first_seal", "999999"))[:6] or 0)), reverse=True)
        leader_map[ind] = {
            "leader": ranked[0].get("code"),
            "second": ranked[1].get("code") if len(ranked) > 1 else None,
        }
    # 行业涨停家数 / 概念涨停家数
    ind_zt = {}
    for s in (snapshot.limit_up if snapshot else []) or []:
        ind = (s.get("industry") or "").strip()
        if ind:
            ind_zt[ind] = ind_zt.get(ind, 0) + 1

    return {
        "concept_weight": concept_weight, "max_cw": max_cw,
        "news_concept": news_concept, "max_news": max_news,
        "sector_pct": sector_pct, "rotation": rotation,
        "limit_map": limit_map, "leader_map": leader_map, "ind_zt": ind_zt,
    }


# ============================================================
#  候选粗筛（快照层，不请求日线）
# ============================================================
def _pre_score(q: dict, code: str, info: dict,
               session: str = "morning") -> float:
    """用行情快照快速排序，选出进入精算的候选（控制请求量）。

    session="noon" 时成交额为半日数据，强度按 ×2 估算做可比处理。
    """
    cp = q.get("change_pct", 0) or 0
    lim = limit_pct_of(code)
    ind = (q.get("industry") or "").strip()
    rot = info["rotation"].get(ind)
    amount = (q.get("amount", 0) or 0) * (2 if session == "noon" else 1)
    s = 0.0
    s += _clamp(cp / max(lim * 0.8, 1)) * 8               # 强度
    s += _clamp((q.get("turnover", 0) or 0) / 12) * 4     # 换手活性
    s += _clamp(amount / 3e8) * 5                         # 成交额
    if rot:
        s += _clamp(max(rot.get("change_pct", 0), 0) / 3) * 5
        s += _clamp(rot.get("zt_today", 0) / 5) * 5
    s += _clamp(info["ind_zt"].get(ind, 0) / 5) * 3
    return round(s, 2)


def _hard_exclude(q: dict, code: str,
                  session: str = "morning") -> Optional[str]:
    """风险剔除（硬门槛）：返回剔除原因，None 表示保留。"""
    name = q.get("name", "")
    if q.get("is_st") or "ST" in name or "退" in name:
        return "ST/退市"
    if q.get("is_new") or name.upper().startswith(("N", "C")):
        return "新股"
    if q.get("price", 0) <= 0:
        return "停牌"
    # 午盘只有上午半日成交额，门槛相应下调
    min_amt = 0.4 * 1e8 if session == "noon" else 0.8 * 1e8
    if q.get("amount", 0) < min_amt:
        return "成交额不足"
    turnover_cap = 22 if session == "noon" else 35
    if (q.get("turnover", 0) or 0) > turnover_cap:
        return "换手过热"
    mv = q.get("total_mv") or q.get("circ_mv") or 0
    if mv < 20 or mv > 5000:
        return "市值范围"
    if market_of(code) == "未知":
        return "非A股"
    return None


# ============================================================
#  因子计算
# ============================================================
def _calc_factors(q: dict, code: str, info: dict, feat: Optional[dict],
                  weekly: Optional[dict], themes: Optional[dict],
                  session: str = "morning") -> dict:
    """返回 {因子key: {score,max,note}}；总分为各因子之和。

    session="noon" 时成交额只覆盖上午半日，按 ×2 折算后再与全天阈值比较。
    """
    cp = q.get("change_pct", 0) or 0
    lim = limit_pct_of(code)
    turnover = q.get("turnover", 0) or 0
    amount = q.get("amount", 0) or 0
    amount_eff = amount * (2 if session == "noon" else 1)
    half_tag = "（半日×2）" if session == "noon" else ""
    ind = (q.get("industry") or "").strip()
    rot = info["rotation"].get(ind)

    # ---- F1 趋势动量（15） ----
    if feat:
        t = _clamp(max(feat["ret20"], 0) / 0.2) * 6
        t += _clamp(max(feat["ret5"], 0) / 0.1) * 3
        t += 4 if (feat["close"] > feat["ma"][20][-1]
                   and feat["ma"][5][-1] > feat["ma"][10][-1]) else 0
        t += 2 if (weekly and weekly.get("bull")) else 0
        t_note = (f"20日{feat['ret20']:+.1%}、5日{feat['ret5']:+.1%}"
                  + ("，周线多头" if weekly and weekly.get("bull") else ""))
        quality = "完整"
    else:
        t = _clamp(max(cp, 0) / max(lim, 1)) * 9
        t_note = f"日线缺失，按快照涨幅{cp:+.1f}%估算"
        quality = "降级"
    t = round(min(t, 15), 1)

    # ---- F2 量价配合（12） ----
    vr = feat["vol_ratio"] if feat else 1.0
    if 1.2 <= vr <= 3.0:
        v = 6
    elif 3.0 < vr <= 5.0:
        v = 4
    elif vr < 1.2:
        v = 2
    else:
        v = 1
    v += 4 if 3 <= turnover <= 18 else (2 if 18 < turnover <= 25 else 0)
    v += 2 if cp > 0 else 0
    v = round(min(v, 12), 1)
    v_note = f"量比{vr:.2f}、换手{turnover:.1f}%"

    # ---- F3 流动性（8） ----
    liq = 4 if amount_eff >= 5e8 else (3 if amount_eff >= 2e8 else
                                       (2 if amount_eff >= 1e8 else 1))
    liq += 2 if 2 <= turnover <= 15 else 0
    liq += 2 if 30 <= (q.get("circ_mv") or 0) <= 1500 else 0
    liq = round(min(liq, 8), 1)
    liq_note = (f"成交额{amount / 1e8:.2f}亿{half_tag}，"
                f"流通市值{(q.get('circ_mv') or 0):.0f}亿")

    # ---- F4 板块轮动（20） ----
    if rot:
        r = _clamp(max(rot.get("change_pct", 0), 0) / 3) * 8
        r += _clamp(rot.get("zt_today", 0) / 8) * 6
        r += 4 if rot.get("momentum", 0) > 0 else 0
        r += 2 if rot.get("stage") in ("加速", "升温启动", "走强") else 0
        r_note = (f"{ind}{rot.get('stage', '')}：涨{rot.get('change_pct', 0):.1f}%、"
                  f"今日涨停{rot.get('zt_today', 0)}家、动量{rot.get('momentum', 0):+.1f}")
    else:
        r = _clamp(max(info["sector_pct"].get(ind, 0), 0) / 3) * 8
        r_note = f"{ind}涨幅{info['sector_pct'].get(ind, 0):.1f}%（无轮动明细）"
    r = round(min(r, 20), 1)

    # ---- F5 龙头强度（15） ----
    zt = info["limit_map"].get(code)
    lm = info["leader_map"].get(ind)
    leader_flag = "独立强势"
    if zt:
        bd = zt.get("boards", 0)
        led = 10 * _clamp(bd / 5)
        led += 3 if (zt.get("seal_fund", 0) or 0) >= 3e8 else 0
        led += 2
        leader_flag = f"{bd}板龙头" if bd >= 2 else "首板"
    else:
        led = 0
        if lm and lm.get("second") == code:
            led += 6
            leader_flag = "次龙头"
        elif lm and lm.get("leader") == code:
            led += 8
            leader_flag = "龙头关联"
        if cp >= lim * 0.5:
            led += 4
            if leader_flag == "独立强势":
                leader_flag = "准涨停强势"
        if info["ind_zt"].get(ind, 0) >= 3:
            led += 5
            leader_flag = (leader_flag if leader_flag != "独立强势"
                           else "板块共振")
    led = round(min(led, 15), 1)
    led_note = f"{leader_flag}" + (f"，板块涨停{info['ind_zt'].get(ind, 0)}家"
                                  if info["ind_zt"].get(ind, 0) else "")

    # ---- F6 题材热度（12） ----
    concepts = [c["name"] for c in (themes or {}).get("concepts", [])]
    hot_raw = sum(info["concept_weight"].get(c, 0) for c in concepts)
    news_raw = sum(info["news_concept"].get(c, 0) for c in concepts)
    hot = _clamp(hot_raw / (info["max_cw"] * 2)) * 7
    hot += _clamp(news_raw / (info["max_news"] * 1.2)) * 5
    hot = round(min(hot, 12), 1)
    hit = [c for c in concepts
           if c in info["concept_weight"] or c in info["news_concept"]]
    hit_sorted = sorted(hit, key=lambda c: info["concept_weight"].get(c, 0),
                        reverse=True)[:4]
    hot_note = ("命中热点：" + "、".join(hit_sorted)) if hit_sorted else \
        ("题材：" + "、".join(concepts[:3]) if concepts else "无题材数据")

    # ---- F7 资金强度（8） ----
    cap = _clamp(amount_eff / 10e8) * 3
    if 0.5 <= cp <= lim * 0.7:
        cap += 3
    elif cp > 0:
        cap += 1.5
    cap += 2 if (feat and feat.get("breakout")) else 0
    cap = round(min(cap, 8), 1)
    cap_note = (f"成交额{amount / 1e8:.2f}亿{half_tag}，涨幅{cp:+.1f}%"
                + ("，放量突破" if feat and feat.get("breakout") else ""))

    # ---- F8 风险质量（10，扣分制） ----
    rk = 10.0
    risk_notes = []
    if cp < 0:
        rk -= 2
        risk_notes.append("当日收跌")
    if turnover > 25:
        rk -= 3
        risk_notes.append("换手过热")
    if feat and feat["ret20"] > 0.5:
        rk -= 4
        risk_notes.append("20日涨幅过大")
    if feat and feat["close"] < feat["ma"][20][-1]:
        rk -= 3
        risk_notes.append("跌破MA20")
    if feat and feat["atr_pct"] > 0.09:
        rk -= 2
        risk_notes.append("波动过大")
    if cp >= lim - 1.5:
        rk -= 2
        risk_notes.append("接近涨停，追高风险")
    if amount < 1e8:
        rk -= 3
        risk_notes.append("成交额偏低")
    rk = round(max(rk, 0), 1)
    risk_note = ("风险扣分：" + "、".join(risk_notes)) if risk_notes else "风险可控"

    factors = {
        "trend": {"score": t, "max": 15, "note": t_note},
        "volume": {"score": v, "max": 12, "note": v_note},
        "liquidity": {"score": liq, "max": 8, "note": liq_note},
        "rotation": {"score": r, "max": 20, "note": r_note},
        "leader": {"score": led, "max": 15, "note": led_note},
        "theme": {"score": hot, "max": 12, "note": hot_note},
        "capital": {"score": cap, "max": 8, "note": cap_note},
        "risk": {"score": rk, "max": 10, "note": risk_note},
    }
    for item in factors.values():
        item["score"] = round(float(item["score"]), 1)
        item["max"] = float(item["max"])
    return {"factors": factors, "leader_flag": leader_flag,
            "hit_concepts": hit_sorted, "data_quality": quality,
            "concepts": concepts,
            "risk_level": "低" if rk >= 8 else ("中" if rk >= 5 else "高")}


# ============================================================
#  权重套用（机构策略近似）
# ============================================================
def apply_weights(factors: dict, weights: Optional[dict]) -> tuple:
    """
    按目标权重重新分配因子满分：保持「得分/满分」比例不变，
    只调整各因子在总分中的占比。返回 (新因子dict, 生效满分表)。
    weights 为 None 或空时原样返回（保持默认 15/12/8/20/15/12/8/10）。
    """
    if not weights:
        return factors, {k: {"name": v["name"], "max": v["max"]}
                         for k, v in FACTOR_META.items()}
    out = {}
    meta = {}
    for key, item in factors.items():
        w = max(0.0, float(weights.get(key, 0) or 0))
        old_max = float(item.get("max") or FACTOR_META.get(key, {}).get("max", 1))
        ratio = _clamp(float(item.get("score", 0) or 0) / old_max)
        new_score = round(ratio * w, 1)
        out[key] = {"score": new_score, "max": float(w), "note": item.get("note", "")}
        meta[key] = {"name": FACTOR_META.get(key, {}).get("name", key),
                     "max": float(w)}
    return out, meta


# ============================================================
#  午盘批：上午行情 / 板块强弱 / 午后推荐逻辑
# ============================================================
def _session_ctx(snapshot, quotes: dict, session: str) -> dict:
    """汇总上午盘面（午盘批）：指数、涨跌家数、情绪、板块强弱排名。"""
    idx = (getattr(snapshot, "indices", None) or {}) if snapshot else {}
    pcts = [float(v.get("change_pct", 0) or 0) for v in idx.values()]
    idx_pct = round(sum(pcts) / len(pcts), 2) if pcts else 0.0
    up = sum(1 for q in quotes.values() if (q.get("change_pct") or 0) > 0)
    down = sum(1 for q in quotes.values() if (q.get("change_pct") or 0) < 0)
    traded = up + down
    up_ratio = (up / traded) if traded else 0.5
    sent = (getattr(snapshot, "sentiment", None) or {}) if snapshot else {}
    temp = float(sent.get("temperature", 50) or 50)

    boards = list((getattr(snapshot, "em_industry_boards", None) or [])
                  if snapshot else [])
    boards.sort(key=lambda b: float(b.get("change_pct", 0) or 0), reverse=True)
    strong = [(str(b.get("name", "")), round(float(b.get("change_pct", 0) or 0), 2))
              for b in boards[:5] if b.get("name")]
    weak = [(str(b.get("name", "")), round(float(b.get("change_pct", 0) or 0), 2))
            for b in boards[-3:] if b.get("name")]
    return {
        "session": session,
        "half": "上午" if session == "noon" else "全天",
        "index_pct": idx_pct, "up": up, "down": down,
        "up_ratio": round(up_ratio, 3), "temperature": temp,
        "mood": sent.get("label", "") or "",
        "strong": strong, "weak": weak,
        "sector_rank": {name: i for i, (name, _) in enumerate(strong)},
        "limit_up": len(getattr(snapshot, "limit_up", None) or []) if snapshot else 0,
        "asof": getattr(snapshot, "asof", "") if snapshot else "",
    }


def _afternoon_view(cp: float, lim: float, sector_pct: float, rot: dict,
                    ctx: dict, turnover: float) -> str:
    """按上午盘面给出午后操作要点（可解释、可执行）。"""
    tips = []
    if cp >= lim - 1.5:
        tips.append("已贴近涨停，午后重点看封单稳定性与炸板风险，空仓不追高，持仓可持有")
    elif cp >= lim * 0.5:
        tips.append("上午走势偏强，午后若回踩不破上午均价可持有，放量滞涨则减仓")
    stage = rot.get("stage", "")
    if stage in ("升温启动", "加速", "走强"):
        tips.append(f"板块处于{stage}阶段，午后若出现涨停扩散，优先参与板块龙头")
    elif sector_pct <= 0:
        tips.append("所属板块上午走弱，午后以个股独立强度为主，仓位不宜过重")
    if ctx.get("up_ratio", 0.5) < 0.35:
        tips.append("上午市场普跌、赚钱效应差，午后只做最强主线，控制仓位")
    elif ctx.get("up_ratio", 0.5) > 0.65:
        tips.append("上午普涨、情绪偏暖，午后可适度放大仓位，但需防高位分化")
    if turnover >= 20:
        tips.append("上午换手偏高，注意午后获利盘兑现压力")
    if not tips:
        tips.append("上午表现平稳，午后跟随板块龙头节奏，跌破上午低点止损")
    return "；".join(tips)


def _session_adjust(q: dict, code: str, info: dict, ctx: dict,
                    session: str) -> tuple:
    """午盘批修正：上午强度/板块排名/半日量能/市场宽度 → (加分, 标签, 数据, 说明)。"""
    if session != "noon":
        return 0.0, [], {}, []
    cp = q.get("change_pct", 0) or 0
    lim = limit_pct_of(code)
    turnover = q.get("turnover", 0) or 0
    amount = q.get("amount", 0) or 0
    ind = (q.get("industry") or "").strip()
    sector_pct = float(info["sector_pct"].get(ind, 0) or 0)
    rot = info["rotation"].get(ind) or {}

    bonus, tags = 0.0, []
    bonus += _clamp(cp / max(lim, 1)) * 2.0
    if cp >= lim - 1.5:
        tags.append("上午近涨停")
        bonus += 0.8
    elif cp >= lim * 0.5:
        tags.append("上午强势")
    rank = ctx["sector_rank"].get(ind)
    if rank is not None:
        bonus += max(0.0, 2.0 - rank * 0.3)
        if rank < 3:
            tags.append("上午领涨板块")
    if amount >= 6e8:
        bonus += 1.0
        tags.append("上午放量")
    if turnover >= 8:
        bonus += 0.5
    if ctx.get("up_ratio", 0.5) < 0.35:
        bonus -= 1.2
        tags.append("上午普跌")
    elif ctx.get("up_ratio", 0.5) > 0.65:
        bonus += 0.6
        tags.append("上午普涨")
    bonus = round(max(-3.0, min(4.0, bonus)), 1)

    morning = {
        "change_pct": round(cp, 2), "amount": round(amount / 1e8, 2),
        "turnover": round(turnover, 1), "sector": ind,
        "sector_pct": sector_pct, "stage": rot.get("stage", ""),
        "up_ratio": ctx.get("up_ratio", 0.5),
    }
    notes = [
        f"上午盘面：涨{cp:+.1f}%、成交额{amount / 1e8:.2f}亿、"
        f"换手{turnover:.1f}%；{ind or '所属板块'}上午{sector_pct:+.1f}%"
        + (f"（{rot.get('stage')}）" if rot.get("stage") else ""),
        "午后策略：" + _afternoon_view(cp, lim, sector_pct, rot, ctx, turnover),
    ]
    return bonus, tags, morning, notes


def _session_brief(ctx: dict, session: str) -> str:
    """午盘批的盘面小结（页面展示）。"""
    if session != "noon":
        return ""
    strong = "、".join(f"{n}({p:+.1f}%)" for n, p in ctx.get("strong", [])[:3]) or "暂无"
    weak = "、".join(f"{n}({p:+.1f}%)" for n, p in ctx.get("weak", [])[:2]) or "暂无"
    return (f"午盘批：上午指数均值 {ctx.get('index_pct', 0):+.2f}%，"
            f"上涨 {ctx.get('up', 0)} 家 / 下跌 {ctx.get('down', 0)} 家"
            f"（涨跌比 {ctx.get('up_ratio', 0.5):.2f}），"
            f"情绪 {ctx.get('temperature', 50):.0f}（{ctx.get('mood', '')}），"
            f"上午涨停 {ctx.get('limit_up', 0)} 家；"
            f"领涨板块：{strong}；走弱板块：{weak}。"
            f"下午盘按「上午强度 + 板块排名 + 半日量能」重新排序，"
            f"优先沿上午领涨主线参与，回避冲高回落品种")


# ============================================================
#  主流程
# ============================================================
def recommend(snapshot=None, top_n: int = 20, workers: int = 10,
              progress: Optional[Callable[[str], None]] = None,
              max_candidates: int = 120,
              quotes: Optional[dict] = None,
              weights: Optional[dict] = None,
              industry_cap: int = 3,
              min_score: float = 38,
              session: str = "morning") -> dict:
    """
    开盘前多因子荐股。
    weights / industry_cap / min_score 为机构策略「近似套用」参数：
    不传时与原来的默认行为完全一致。
    session="noon" 时使用上午行情与板块数据重排，并输出午后操作逻辑。
    返回 {picks, ladder, rotation, asof, trade_date, generated_at,
          degraded, data_notes, universe, candidates, factors_meta}。
    """
    t0 = dt.datetime.now()
    notes: List[str] = []
    degraded = False

    def log(m):
        if progress:
            progress(m)

    if snapshot is None:
        snapshot = load_latest_snapshot()
        if snapshot is not None:
            notes.append("快照来自本地缓存")
            degraded = True
    if snapshot is None:
        snapshot = market_info.MarketSnapshot(
            asof=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_date=market_info.recent_trade_dates(1)[0])
        notes.append("无市场快照：降级为行情+技术面选股")
        degraded = True

    info = _build_info_features(snapshot)
    if snapshot.news_sources:
        notes.append("新闻源：" + "、".join(snapshot.news_sources))

    log("获取全市场实时行情（东财清单，盘中强制刷新）…")
    if quotes is None:
        try:
            quotes = fetch_all_quotes(force=is_trading_time())
        except Exception:
            try:
                quotes = fetch_all_quotes(force=False)
                notes.append("实时行情失败，降级使用本地缓存行情")
                degraded = True
            except Exception as e:  # noqa
                return {
                    "picks": [], "ladder": [], "rotation": [],
                    "asof": snapshot.asof, "trade_date": snapshot.trade_date,
                    "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "degraded": True, "data_notes": notes + [f"行情不可用：{e}"],
                    "universe": 0, "candidates": 0,
                    "factors_meta": FACTOR_META,
                }
    if not is_trading_time():
        notes.append("非交易时段：行情为最近一个交易日收盘数据")

    ctx = _session_ctx(snapshot, quotes, session)
    if session == "noon":
        notes.append("午盘批：使用上午收盘行情与上午板块强弱，面向 13:00 后下午盘")

    log(f"全市场 {len(quotes)} 只，风险剔除并按强度粗筛…")
    pool = []
    excluded = {}
    for code, q in quotes.items():
        why = _hard_exclude(q, code, session)
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        pool.append((code, q, _pre_score(q, code, info, session)))
    pool.sort(key=lambda x: x[2], reverse=True)
    cands = pool[:max(max_candidates, top_n * 3)]
    log(f"候选 {len(cands)} 只（剔除："
        + "、".join(f"{k}{v}只" for k, v in excluded.items()) + "）")

    # 涨停梯队龙头强制进入候选，避免因涨幅门槛被粗筛漏掉
    leader_codes = [s.get("code") for s in (snapshot.limit_up or [])
                    if (s.get("boards", 0) >= 2
                        or (s.get("seal_fund", 0) or 0) >= 3e8)]
    cand_codes = [c for c, _, _ in cands]
    for code in leader_codes:
        if code not in cand_codes and code in quotes:
            q = quotes[code]
            if _hard_exclude(q, code, session) is None:
                cands.append((code, q, _pre_score(q, code, info, session)))

    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=520)).strftime("%Y%m%d")

    def load_feat(code):
        try:
            df = fetch_daily_fast(code, start, end)
            f = daily_features(df)
            w = _weekly_features(df) if df is not None else None
            return code, f, w
        except Exception:
            return code, None, None

    def load_theme(code):
        return code, fetch_core_themes(code)

    log(f"并发获取日线与题材（{len(cands)} 只候选）…")
    feats, weeklys, theme_map = {}, {}, {}
    theme_codes = [c for c, _, _ in cands[:max(80, top_n * 4)]]
    with ThreadPoolExecutor(max_workers=max(6, min(workers, 12))) as ex:
        fu_daily = [ex.submit(load_feat, c) for c, _, _ in cands]
        fu_theme = [ex.submit(load_theme, c) for c in theme_codes]
        done = 0
        for fut in as_completed(fu_daily):
            try:
                code, f, w = fut.result()
            except Exception:
                continue
            if f is not None:
                feats[code] = f
                weeklys[code] = w
            done += 1
            if done % 30 == 0:
                log(f"日线进度 {done}/{len(cands)}")
        for fut in as_completed(fu_theme):
            try:
                code, themes = fut.result()
                theme_map[code] = themes
            except Exception:
                continue

    log("多因子打分…")
    picks = []
    effective_meta = {k: {"name": v["name"], "max": v["max"]}
                      for k, v in FACTOR_META.items()}
    for code, q, pre in cands:
        themes = theme_map.get(code)
        res = _calc_factors(q, code, info, feats.get(code),
                            weeklys.get(code), themes, session)
        # 机构策略套用：按目标权重重分配满分（默认权重时与原始满分一致）
        factors, effective_meta = apply_weights(res["factors"], weights)
        base_total = sum(v["score"] for v in factors.values())
        # 午盘批：上午强度 + 板块排名 + 半日量能修正（收盘批不生效）
        s_bonus, s_tags, morning, s_notes = _session_adjust(
            q, code, info, ctx, session)
        total = round(min(100.0, base_total + s_bonus), 1)
        reasons = []
        for key in ("rotation", "trend", "volume", "leader", "theme", "capital"):
            note = factors[key]["note"]
            if note:
                reasons.append(f"{FACTOR_META[key]['name']}：{note}")
        if res["hit_concepts"]:
            reasons.append("题材共振：" + "、".join(res["hit_concepts"]))
        reasons.extend(s_notes)
        reasons.append(factors["risk"]["note"])
        if res["data_quality"] == "降级":
            reasons.append("数据降级：日线不可用，仅按行情快照打分")

        base_tags = ["周线多头"] if (weeklys.get(code)
                                    and weeklys[code].get("bull")) else []
        picks.append(Pick(
            rank=0, code=code, name=q["name"], market=market_of(code),
            price=q["price"], change_pct=q["change_pct"], score=total,
            industry=(q.get("industry") or
                      (themes["industry"][-1] if themes and themes["industry"]
                       else "")),
            turnover=q.get("turnover", 0) or 0,
            amount=q.get("amount", 0) or 0,
            total_mv=q.get("total_mv") or q.get("circ_mv") or 0,
            hit_concepts=res["hit_concepts"], leader_flag=res["leader_flag"],
            risk=res["risk_level"], reasons=reasons,
            factors=factors,
            factor_scores={k: v["score"] for k, v in factors.items()},
            tags=base_tags + s_tags,
            data_quality=res["data_quality"],
            pe=float(q.get("pe") or 0),
            pb=float(q.get("pb") or 0),
            chg60=float(q.get("chg60") or 0),
            chg_ytd=float(q.get("chg_ytd") or 0),
            rotation_score=factors["rotation"]["score"],
            leader_score=factors["leader"]["score"],
            hot_score=factors["theme"]["score"],
            news_score=factors["theme"]["score"],
            tech_score=factors["trend"]["score"],
            session_bonus=s_bonus,
            session_note=("；".join(s_notes) if s_notes else ""),
            morning=morning,
        ))

    # 弱市收紧：情绪低迷或涨跌宽度过差时，提高入选门槛并减少推荐数量，
    # 这是针对回测中「最大回撤偏大、胜率不足 50%」的主要风险点做的保护
    eff_min_score = min_score
    top_eff = top_n
    weak_mood = (ctx.get("temperature", 50) <= 35
                 or ctx.get("up_ratio", 0.5) < 0.30)
    if weak_mood:
        eff_min_score += 6
        top_eff = max(5, top_n // 2)
        notes.append(
            f"弱市模式：情绪 {ctx.get('temperature', 50):.0f}、"
            f"上涨占比 {ctx.get('up_ratio', 0.5):.0%}，"
            f"入选门槛上调 6 分、推荐数量减半以控制回撤")

    # 硬门槛：综合分过低且无任何题材/轮动支撑的不推荐（避免凑数）
    picks = [p for p in picks
             if p.score >= eff_min_score or p.leader_flag.endswith("龙头")]
    picks.sort(key=lambda p: (p.score, p.factor_scores.get("rotation", 0)),
               reverse=True)

    # 行业分散：同行业最多 industry_cap 只
    per_sector, filtered = {}, []
    for p in picks:
        n = per_sector.get(p.industry, 0)
        if n >= max(1, int(industry_cap)):
            continue
        per_sector[p.industry] = n + 1
        filtered.append(p)
    picks = filtered

    top = picks[:top_eff]
    for i, p in enumerate(top, 1):
        p.rank = i

    ladder = sorted(snapshot.limit_up or [],
                    key=lambda s: (s.get("boards", 0),
                                  s.get("seal_fund", 0)), reverse=True)
    ladder_out = [{
        "code": s.get("code"), "name": s.get("name"),
        "market": market_of(s.get("code", "")),
        "price": s.get("price"), "boards": s.get("boards", 0),
        "reasons": s.get("reasons", []),
        "break_count": s.get("break_count", 0),
        "seal_fund": round((s.get("seal_fund", 0) or 0) / 1e8, 2),
    } for s in ladder]

    rotation_list = sorted(
        (dict(name=k, **v) for k, v in info["rotation"].items()
         if v.get("zt_today", 0) > 0 or v.get("change_pct", 0) >= 1.5),
        key=lambda r: (r.get("zt_today", 0), r.get("change_pct", 0)),
        reverse=True)[:12]

    elapsed = (dt.datetime.now() - t0).total_seconds()
    if not feats:
        notes.append("全部日线获取失败：已全部降级为快照打分")
        degraded = True
    log(f"多因子选股完成：{len(top)} 只（候选 {len(cands)}），"
        f"耗时 {elapsed:.1f}s")

    return {
        "asof": snapshot.asof, "trade_date": snapshot.trade_date,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "picks": [p.__dict__ for p in top],
        "ladder": ladder_out,
        "rotation": rotation_list,
        "degraded": degraded,
        "data_notes": notes,
        "session": session,
        "session_ctx": ctx,
        "session_brief": _session_brief(ctx, session),
        "universe": len(quotes), "candidates": len(cands),
        "factors_meta": effective_meta,
        "excluded": excluded,
        "elapsed": round(elapsed, 1),
    }


if __name__ == "__main__":
    snap = market_info.refresh_all(progress=lambda m: print(m))
    res = recommend(snap, progress=lambda m: print(m))
    print("\n==> 多因子 Top：")
    for p in res["picks"]:
        fs = p["factor_scores"]
        print(f"#{p['rank']} {p['code']} {p['name']} 分{p['score']:.1f} "
              f"涨{p['change_pct']:+.1f}% {p['industry']} | "
              + " ".join(f"{FACTOR_META[k]['name']}{fs[k]:.0f}"
                        for k in FACTOR_META))
        for r in p["reasons"][:3]:
            print("    -", r)
    print("\ndegraded:", res["degraded"], res["data_notes"])
