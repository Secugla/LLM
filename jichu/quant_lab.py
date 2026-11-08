"""量化研究工具：因子 IC/分层、参数稳健性、滚动回归模型、收益归因与历史记录。

本模块只做研究验证，不直接下单；数据全部来自本地已接入的免费行情源。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

import backtest_service as bs
import market_info as mi
import strategy_library as SL
from data_sources import fetch_all_quotes

HISTORY_PATH = Path(__file__).resolve().parent / ".cache" / "backtest_history.jsonl"


# ============================================================
#  数据集构建：按月/按 horizon 取横截面因子与未来收益
# ============================================================
def _load_dataset(months: int = 18, pool_size: int = 200, horizon: int = 20,
                  neutralize: bool = True, strategy_id: str = "high_flyer",
                  fund_boost: bool = True,
                  progress: Optional[Callable[[str], None]] = None,
                  cancel: Optional[threading.Event] = None) -> tuple:
    log = progress or (lambda s: None)
    start, end = bs._range_from_months(months)
    log("拉取全市场快照并筛选股票池…")
    quotes = fetch_all_quotes()
    pool = bs._load_pool(quotes, pool_size)
    log(f"股票池 {len(pool)} 只，加载日线…")
    data = bs._fetch_pool_daily(pool, start, end, log, cancel)
    if not data:
        raise ValueError("股票池日线为空，无法做因子检验")

    dates = sorted({d for df in data.values() for d in df.index})
    dates = pd.DatetimeIndex([d for d in dates if start <= d <= end])
    if len(dates) < horizon * 4:
        raise ValueError("有效交易日太少，请缩短 horizon 或延长回测区间")

    close_mat = pd.DataFrame({c: df["close"] for c, df in data.items()}) \
        .reindex(dates).ffill()
    weights = SL.get_strategy(strategy_id)["weights"]

    fund_data = {}
    seat_maps = {}
    if fund_boost:
        try:
            fund_data = mi.fund_flow_range(f"{start:%Y-%m-%d}",
                                           f"{end:%Y-%m-%d}", log)
            step0 = max(5, int(horizon))
            dstr = [dates[i].strftime("%Y-%m-%d")
                    for i in range(0, len(dates) - horizon, step0)]
            seat_maps = mi.seat_quality_range(dstr, log)
        except Exception:
            fund_data, seat_maps = {}, {}

    frames = []
    step = max(5, int(horizon))
    for idx in range(0, len(dates) - horizon, step):
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        date = dates[idx]
        log(f"因子打分 {date.strftime('%Y-%m-%d')}…")
        ds = date.strftime("%Y-%m-%d")
        fund = None
        if fund_data:
            fund = {"lhb": (fund_data.get("lhb") or {}).get(ds, {}),
                    "north": (fund_data.get("north") or {}).get(ds, {})}
            if seat_maps:
                fund["seat"] = seat_maps.get(ds, {})
        picks, _ = bs._score_universe_at(
            date, data, quotes, weights, neutralize=neutralize, fund=fund)
        if len(picks) < 20:
            continue
        fwd = close_mat.iloc[idx + horizon] / close_mat.iloc[idx] - 1
        rows = []
        for p in picks:
            r = float(fwd.get(p["code"], np.nan))
            if not np.isfinite(r) or abs(r) > 1.0:
                continue
            row = {"code": p["code"], "total": float(p["total"]),
                   "fwd": r}
            for k in weights:
                row[k + "_pct"] = float(p["factors"][k].get("pct", 0.5))
            for k in ("lhb", "north"):
                if k in p["factors"]:
                    row[k + "_pct"] = float(p["factors"][k].get("pct", 0.0))
            rows.append(row)
        if len(rows) >= 20:
            frames.append((date, pd.DataFrame(rows)))
    if len(frames) < 3:
        raise ValueError("有效横截面样本不足，无法计算 IC")
    return frames, weights, len(data), len(quotes), start, end


def _rank_corr(a, b) -> float:
    """Spearman 秩相关（不依赖 scipy）。"""
    sa = pd.Series(np.asarray(a, dtype=float)).rank()
    sb = pd.Series(np.asarray(b, dtype=float)).rank()
    v = sa.corr(sb)
    return float(v) if np.isfinite(v) else 0.0


def _ic_stats(series: list) -> dict:
    arr = pd.Series([x for x in series if np.isfinite(x)], dtype=float)
    if len(arr) == 0:
        return {"mean": None, "ir": None, "positive": None, "n": 0}
    mean = float(arr.mean())
    ir = float(arr.mean() / arr.std()) if arr.std() > 1e-9 else None
    return {"mean": round(mean, 4), "ir": None if ir is None else round(ir, 2),
            "positive": round(float((arr > 0).mean() * 100), 1),
            "n": int(len(arr))}


# ============================================================
#  1. 因子有效性：IC / RankIC / 分层收益
# ============================================================
def factor_analysis(months: int = 18, pool_size: int = 200, horizon: int = 20,
                    neutralize: bool = True, strategy_id: str = "high_flyer",
                    fund_boost: bool = True,
                    progress: Optional[Callable[[str], None]] = None,
                    cancel: Optional[threading.Event] = None) -> dict:
    frames, weights, loaded, total_quotes, start, end = _load_dataset(
        months, pool_size, horizon, neutralize, strategy_id, fund_boost,
        progress, cancel)

    has_fund = any("lhb_pct" in df.columns for _d, df in frames)
    keys = list(weights)
    wmap = {k: float(v) for k, v in weights.items()}
    if has_fund:
        half = round(100 * bs.FUND_WEIGHT / 2, 1)
        keys += ["lhb", "north"]
        wmap["lhb"] = half
        wmap["north"] = half
    extra_name = {"lhb": "龙虎榜", "north": "北向关注"}

    ic_map = {k: [] for k in keys}
    rank_ic_map = {k: [] for k in keys}
    layers = [[] for _ in range(5)]

    for _date, df in frames:
        for k in keys:
            col = k + "_pct"
            if col not in df.columns or df[col].nunique() < 3:
                continue
            ic_map[k].append(df[col].corr(df["fwd"], method="pearson"))
            rank_ic_map[k].append(_rank_corr(df[col], df["fwd"]))
        ranked = df["total"].rank(method="first")
        q = pd.qcut(ranked, 5, labels=False)
        for qi, grp in df.groupby(q):
            layers[int(qi)].append(float(grp["fwd"].mean()))

    factors = []
    for k in keys:
        ic = _ic_stats(ic_map[k])
        ric = _ic_stats(rank_ic_map[k])
        factors.append({
            "key": k, "name": SL.FACTOR_NAME.get(k, extra_name.get(k, k)),
            "weight": round(float(wmap[k]), 1),
            "ic": ic["mean"], "ic_ir": ic["ir"], "ic_positive": ic["positive"],
            "rank_ic": ric["mean"], "rank_ic_ir": ric["ir"],
            "rank_ic_positive": ric["positive"],
            "samples": ric["n"],
        })
    factors.sort(key=lambda x: abs(x["rank_ic"] or 0), reverse=True)

    layer_rows = []
    for i, rets in enumerate(layers):
        arr = np.array([x for x in rets if np.isfinite(x)], dtype=float)
        if len(arr) == 0:
            continue
        avg = float(arr.mean())
        ann = (1 + avg) ** (252 / horizon) - 1
        layer_rows.append({
            "layer": f"Q{i + 1}",
            "label": ["最低分", "偏低", "中等", "偏高", "最高分"][i],
            "avg_return": round(avg * 100, 2),
            "annual_return": round(ann * 100, 2),
            "win_rate": round(float((arr > 0).mean() * 100), 1),
            "samples": int(len(arr)),
        })

    spread = None
    if layer_rows:
        spread = round(layer_rows[-1]["annual_return"]
                       - layer_rows[0]["annual_return"], 2)

    return {
        "period": f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}",
        "months": months, "horizon": horizon, "neutralize": neutralize,
        "fund_boost": has_fund, "fund_weight": bs.FUND_WEIGHT,
        "strategy": SL.get_strategy(strategy_id)["name"],
        "samples": len(frames), "pool_loaded": loaded,
        "universe_quotes": total_quotes, "factors": factors,
        "layers": layer_rows, "top_bottom_spread": spread,
    }


# ============================================================
#  2. 参数稳健性：入场阈值 × 移动止损 网格 + 样本外
# ============================================================
def param_grid(code: str, months: int = 18, init_cash: int = 200000,
               strategy_id: str = "high_flyer", fund_boost: bool = True,
               progress: Optional[Callable[[str], None]] = None,
               cancel: Optional[threading.Event] = None) -> dict:
    log = progress or (lambda s: None)
    entries = [55, 60, 65]
    trails = [0.10, 0.13, 0.16]
    oos_months = max(3, int(round(months * 0.35)))
    rows = []
    total_runs = len(entries) * len(trails)
    run = 0
    pick = None
    for entry in entries:
        for trail in trails:
            if cancel and cancel.is_set():
                raise InterruptedError("已取消")
            run += 1
            log(f"参数网格 {run}/{total_runs}：入场 {entry} / 止损 {trail:.0%}")
            full = bs.run_single_stock(
                code, months, init_cash, strategy_id,
                entry_threshold=float(entry), trail_stop=trail,
                fund_boost=fund_boost, progress=None, cancel=cancel)
            oos = bs.run_single_stock(
                code, oos_months, init_cash, strategy_id,
                entry_threshold=float(entry), trail_stop=trail,
                fund_boost=fund_boost, progress=None, cancel=cancel)
            pick = pick or {"name": full.get("name"), "code": code}
            fm, om = full["metrics"], oos["metrics"]

            def sub(m):
                return {k: m.get(k) for k in (
                    "total_return", "excess_return", "max_drawdown",
                    "sharpe", "win_rate", "profit_factor", "trades")}

            rows.append({
                "entry": entry, "trail": round(trail * 100),
                "full": sub(fm), "oos": sub(om),
            })
    best = max(rows, key=lambda x: (x["full"].get("excess_return") or -999))
    overfit = (best["full"].get("excess_return") or 0) > 5 \
        and (best["oos"].get("excess_return") or 0) < 0
    return {
        "code": code, "name": (pick or {}).get("name", ""),
        "months": months, "oos_months": oos_months,
        "entries": entries, "trails": [int(t * 100) for t in trails],
        "rows": rows, "best": best, "overfit": bool(overfit),
        "note": "样本外为回测区间最后 35% 的独立区间；参数选择应优先参考样本外。",
    }


# ============================================================
#  3. 机器学习：滚动训练线性回归（Ridge）
# ============================================================
def ml_walkforward(months: int = 18, pool_size: int = 200, horizon: int = 20,
                   neutralize: bool = True, strategy_id: str = "high_flyer",
                   fund_boost: bool = True,
                   progress: Optional[Callable[[str], None]] = None,
                   cancel: Optional[threading.Event] = None) -> dict:
    frames, weights, loaded, _, start, end = _load_dataset(
        months, pool_size, horizon, neutralize, strategy_id, fund_boost,
        progress, cancel)
    feats = [k + "_pct" for k in weights]
    if any("lhb_pct" in f.columns for _d, f in frames):
        feats += ["lhb_pct", "north_pct"]
    feat_name = dict(SL.FACTOR_NAME)
    feat_name.update({"lhb": "龙虎榜", "north": "北向关注"})
    min_train = 3
    rank_ics, top_excess, models = [], [], []
    baseline_ics = []

    for i in range(min_train, len(frames)):
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        train_dates = [d for d, _ in frames[:i]]
        train = pd.concat([f for _, f in frames[:i]], ignore_index=True)
        X = np.column_stack([np.ones(len(train)),
                             train[feats].fillna(0.0).to_numpy(float)])
        y = train["fwd"].to_numpy(float)
        lam = 5.0
        A = X.T @ X + lam * np.eye(X.shape[1])
        A[0, 0] -= lam                      # 截距不惩罚
        try:
            beta = np.linalg.solve(A, X.T @ y)
        except np.linalg.LinAlgError:
            continue
        cur = frames[i][1]
        Xc = np.column_stack([np.ones(len(cur)),
                              cur[feats].fillna(0.0).to_numpy(float)])
        pred = Xc @ beta
        rank_ics.append(_rank_corr(pred, cur["fwd"]))
        top = float(cur.loc[pred >= np.quantile(pred, 0.8), "fwd"].mean())
        top_excess.append(top - float(cur["fwd"].mean()))
        baseline_ics.append(_rank_corr(cur["total"], cur["fwd"]))
        models.append({"date": frames[i][0].strftime("%Y-%m-%d"),
                       "train_from": train_dates[0].strftime("%Y-%m-%d"),
                       "train_rows": int(len(train)), "beta": beta[1:]})

    stats = _ic_stats(rank_ics)
    base = _ic_stats(baseline_ics)
    coef = np.mean([m["beta"] for m in models], axis=0) if models else np.zeros(len(feats))
    denom = float(np.abs(coef).sum()) or 1.0
    importance = sorted(
        [{"name": feat_name.get(k[:-4], k), "weight": round(float(c) / denom, 1)}
         for k, c in zip(feats, coef)],
        key=lambda x: abs(x["weight"]), reverse=True)
    avg_excess = float(np.mean(top_excess)) if top_excess else None

    return {
        "period": f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}",
        "months": months, "horizon": horizon, "neutralize": neutralize,
        "strategy": SL.get_strategy(strategy_id)["name"],
        "samples": len(frames), "windows": len(models),
        "rank_ic": stats["mean"], "ic_ir": stats["ir"],
        "positive": stats["positive"], "baseline_rank_ic": base["mean"],
        "top_excess": None if avg_excess is None
        else round(avg_excess * 100, 2),
        "importance": importance,
        "note": "滚动窗口只用当前时点之前已到期的样本训练，避免未来函数。",
    }


# ============================================================
#  4. 收益归因
# ============================================================
def attribution(result: dict) -> dict:
    quotes = {}
    try:
        quotes = fetch_all_quotes()
    except Exception:
        pass
    agg = {}
    for t in result.get("trades", []):
        if t.get("side") != "卖出":
            continue
        code = t.get("code")
        a = agg.setdefault(code, {
            "code": code,
            "name": t.get("name") or quotes.get(code, {}).get("name", ""),
            "industry": quotes.get(code, {}).get("industry") or "未分类",
            "pnl": 0.0, "trades": 0, "wins": 0,
        })
        pnl = float(t.get("pnl") or 0)
        a["pnl"] += pnl
        a["trades"] += 1
        a["wins"] += 1 if pnl > 0 else 0
    init = float(result.get("init_cash") or 0)
    total_pnl = sum(a["pnl"] for a in agg.values())
    for a in agg.values():
        a["pnl"] = round(a["pnl"], 2)
        a["win_rate"] = round(a["wins"] / a["trades"] * 100, 1) if a["trades"] else 0
        a["contribution"] = round(a["pnl"] / init * 100, 2) if init else 0.0
        a["share"] = round(a["pnl"] / total_pnl * 100, 1) if total_pnl else 0.0
    stocks = sorted(agg.values(), key=lambda x: abs(x["pnl"]), reverse=True)
    ind = {}
    for a in agg.values():
        g = ind.setdefault(a["industry"], {"industry": a["industry"],
                                           "pnl": 0.0, "stocks": 0})
        g["pnl"] += a["pnl"]
        g["stocks"] += 1
    industries = []
    for g in sorted(ind.values(), key=lambda x: abs(x["pnl"]), reverse=True):
        g["pnl"] = round(g["pnl"], 2)
        g["contribution"] = round(g["pnl"] / init * 100, 2) if init else 0.0
        industries.append(g)
    return {"total_pnl": round(total_pnl, 2), "stocks": stocks,
            "industries": industries}


# ============================================================
#  5. 模型版本 / 回测历史记录
# ============================================================
def log_backtest(result: dict, att: Optional[dict] = None) -> dict:
    if att is None:
        try:
            att = attribution(result)
        except Exception:
            att = {"stocks": [], "industries": [], "total_pnl": 0}
    params = {
        "months": result.get("months"), "init_cash": result.get("init_cash"),
        "strategy_id": result.get("strategy_id"),
        "strategy_name": result.get("strategy_name"),
    }
    if result.get("mode") == "universe":
        params.update({"top_n": result.get("top_n"),
                       "pool_size": result.get("pool_size"),
                       "rebalance_days": result.get("rebalance_days")})
    else:
        params.update({"code": result.get("code"),
                       "entry_threshold": result.get("entry_threshold"),
                       "exit_threshold": result.get("exit_threshold")})
    # 模型版本：按因子权重快照生成 8 位指纹，便于追溯"哪套权重跑出的结果"
    try:
        wts = SL.get_strategy(params.get("strategy_id") or "")["weights"]
        params["weights"] = wts
        params["model_version"] = hashlib.md5(
            json.dumps(wts, sort_keys=True).encode()).hexdigest()[:8]
    except Exception:
        pass
    rec = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": result.get("mode"), "mode_name": result.get("mode_name"),
        "title": result.get("name") or result.get("code") or "全市场组合",
        "params": params,
        "flags": {"neutralize": result.get("neutralize"),
                  "exec_constraints": result.get("exec_constraints"),
                  "fund_boost": result.get("fund_boost")},
        "metrics": {k: result.get("metrics", {}).get(k) for k in (
            "total_return", "bench_return", "excess_return", "max_drawdown",
            "sharpe", "win_rate", "profit_factor", "trades")},
        "exec_stats": result.get("exec_stats"),
        "attribution": att.get("stocks", [])[:6],
        "industries": att.get("industries", [])[:5],
    }
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def load_history(limit: int = 40) -> list:
    if not HISTORY_PATH.exists():
        return []
    rows = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        return []
    return list(reversed(rows))[:limit]
