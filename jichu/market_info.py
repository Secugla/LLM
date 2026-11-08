# -*- coding: utf-8 -*-
"""
信息抓取与事件分析引擎
======================
对应「信息抓取」功能模块，同时为「当日荐股」提供信息底座。

本次重构要点：
  · 多源适配 + 自动降级：东财 / 同花顺 / 财联社 / 新浪 / 腾讯互为备份，
    任一源改版或超时不会拖垮整体。
  · 磁盘 TTL 缓存：指数 5s、板块 60s、涨停池 90s、新闻 90s、F10 题材 7 天，
    大幅减少重复请求。
  · 流式并发：refresh_all_stream 每个数据块完成即回调渲染（界面秒开、分区填充），
    总预算 30 秒，单个任务超时直接跳过，不阻塞其他区块。
  · 抓取内容：大盘指数、涨停/跌停池、涨停原因、行业/概念板块、财经新闻、
    热点概念聚类、情绪温度、板块轮动。

所有抓取带超时、重试上限、缓存与优雅降级；单个源失败不影响整体。
"""

import datetime as dt
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional
from urllib.parse import urlencode

import requests

from data_sources import cache_load, cache_save, is_trading_time

# 全局外发请求并发闸门：免费接口有限速，无论上层开多少个线程池，
# 实际同时进行的 HTTP 请求最多 8 个，避免多线程池互相挤爆、集体超时。
HTTP_GATE = threading.Semaphore(8)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(PROJECT_DIR, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)
# 最新一次完整市场快照（启动时先读它立即渲染，再后台刷新覆盖）
LATEST_SNAPSHOT = os.path.join(CACHE_DIR, "latest_snapshot.json")

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
_EM_UT = "7eea3edcaed734bea9cbfc24409ed989"

# 东财 F10 会混入大量「统计标签 / 宽基属性」，并非真正的题材概念，需过滤，
# 否则热点概念聚类会被「融资融券 / 沪股通 / QFII重仓」等淹没。
NOISE_CONCEPTS = {
    "融资融券", "融资融券标的", "转融券标的", "沪股通", "深股通", "QFII重仓",
    "QFII新进", "机构重仓", "基金重仓", "基金增仓", "社保重仓", "养老金持股",
    "养老金", "MSCI中国", "MSCI概念", "标普道琼斯A股", "富时罗素概念股", "富时罗素",
    "东方财富热股", "最近多板", "昨日涨停", "昨日涨停_含一字", "昨日连板",
    "昨日连板_含一字", "昨日高振幅", "昨日打二板以上表现", "昨日炸板", "昨日触板",
    "昨日跌停", "题材股", "百日新高", "近期新高", "新股与次新股", "注册制次新股",
    "核准制次新股", "举牌", "高送转", "高送转预期", "已高送转", "送转预期",
    "证金持股", "汇金概念", "超涨", "超跌", "破净股", "低价股", "高价股",
    "小盘股", "大盘股", "强势股", "人气龙头", "ST板块", "两网及退市", "股权激励",
    "分红", "机构自营", "中字头", "综合", "创投概念",
    "百元股", "低价股", "高价股", "微盘股", "次新股", "近期摘帽", "高成长股",
    "连续亏损", "深成500", "上证380", "中证500", "沪深300", "上证50", "中证1000",
    "创业板综", "科创50", "成份股", "内贸流通", "标普概念", "富时罗素概念",
    "昨日首板", "基金减仓", "基金增仓", "基金新进", "基金减持", "基金增持",
    "机构减仓", "机构增仓", "机构新进", "2026中报首亏",
    "标准普尔", "小盘成长", "中盘成长", "大盘成长", "小盘价值", "大盘价值",
    "破增发价股", "破发股", "信托重仓", "高市净率", "低市净率", "高市盈率",
    "低市盈率", "破净股", "高估值", "低估值", "成长股", "价值股",
}

# 动态年份/统计标签无法用固定集合穷举，用子串兜底
_NOISE_SUBSTR = ["首亏", "中报", "季报", "年报", "基金减", "基金增", "机构减",
                 "机构增", "昨日", "首板", "业绩预", "预亏", "预盈", "预增",
                 "预减", "高振幅", "中报首", "报表", "破发", "增发", "市净率",
                 "市盈率", "小盘", "中盘", "大盘", "重仓", "破增", "标准普尔",
                 "百元股", "微盘", "次新", "摘帽", "连续亏损", "成长股", "成份股",
                 "500", "300", "380", "1000", "综指"]


def _is_noise_concept(name: str) -> bool:
    n = name.strip()
    if n in NOISE_CONCEPTS:
        return True
    return any(k in n for k in _NOISE_SUBSTR)


# ============================================================
#  数据结构
# ============================================================
@dataclass
class NewsItem:
    title: str
    summary: str = ""
    time: str = ""
    url: str = ""
    category: str = "其他"
    sentiment: float = 0.0          # -1(利空) ~ 1(利好)
    concepts: List[str] = field(default_factory=list)
    source: str = ""


@dataclass
class LimitUpStock:
    code: str
    name: str
    price: float
    change_pct: float
    amount: float                   # 成交额（元）
    circ_mv: float                  # 流通市值（元）
    turnover: float                 # 换手率 %
    seal_fund: float                # 封板资金（元）
    first_seal: str                 # 首次封板时间 HHMMSS
    last_seal: str
    break_count: int                # 炸板次数
    boards: int                     # 连板数
    stat: str                       # 涨停统计 N/N
    industry: str
    concepts: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)   # 主要驱动概念
    reason_detail: str = ""         # 驱动概念的理由


@dataclass
class SectorInfo:
    name: str
    change_pct: float
    lead: str = ""
    stock_count: int = 0


@dataclass
class MarketSnapshot:
    asof: str
    trade_date: str
    indices: dict = field(default_factory=dict)
    limit_up: List[dict] = field(default_factory=list)
    limit_down_count: int = 0
    sectors: List[dict] = field(default_factory=list)
    em_industry_boards: List[dict] = field(default_factory=list)
    em_concept_boards: List[dict] = field(default_factory=list)
    hot_concepts: List[dict] = field(default_factory=list)
    news: List[dict] = field(default_factory=list)
    news_sources: List[str] = field(default_factory=list)
    sentiment: dict = field(default_factory=dict)
    rotation: List[dict] = field(default_factory=list)
    lhb: List[dict] = field(default_factory=list)
    northbound: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    from_cache: bool = False
    errors: List[str] = field(default_factory=list)

    def to_json(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketSnapshot":
        """缓存 dict → MarketSnapshot（缺失字段用默认值兜底）。"""
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in fields})


# ============================================================
#  工具函数
# ============================================================
def _retry_get(url, params=None, headers=None, timeout=8, retries=1,
               encoding=None):
    """带重试的 GET（重试上限 1 次，超时优先保证整体不卡住）。"""
    last = None
    headers = headers or _UA
    for i in range(retries + 1):
        try:
            with HTTP_GATE:
                r = requests.get(url, params=params, headers=headers,
                                 timeout=(4, timeout))
            if encoding:
                r.encoding = encoding
            return r
        except Exception as e:  # noqa
            last = e
            time.sleep(0.25)
    raise last


def secucode(code: str) -> str:
    """6位代码 → 东财 F10 用的 SECUCODE（带交易所后缀）。"""
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def market_of(code: str) -> str:
    """代码 → 交易所名称。"""
    if code.startswith(("60", "68")):
        return "上交所"
    if code.startswith(("00", "30")):
        return "深交所"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "未知"


def recent_trade_dates(n=6, ref: Optional[dt.date] = None) -> List[str]:
    """最近 n 个候选交易日（跳过周末），返回 YYYYMMDD。"""
    d = ref or dt.date.today()
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= dt.timedelta(days=1)
    return out


def _hhmmss(x) -> str:
    try:
        return str(int(x)).zfill(6)
    except Exception:
        return "000000"


def _assert_list(v) -> list:
    return v if isinstance(v, list) else []


_ZT_FIELDS = {f.name for f in LimitUpStock.__dataclass_fields__.values()}


def _zt_obj(d: dict) -> LimitUpStock:
    """dict → LimitUpStock（忽略 date 等快照附加字段）。"""
    return LimitUpStock(**{k: v for k, v in d.items() if k in _ZT_FIELDS})


# ============================================================
#  1. 大盘指数（新浪 → 东方财富兜底）
# ============================================================
_INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    "bj899050": "北证50",
}

_EM_INDEX_SECIDS = {
    "1.000001": "上证指数", "0.399001": "深证成指",
    "0.399006": "创业板指", "1.000688": "科创50", "0.899050": "北证50",
}


def fetch_indices() -> dict:
    """大盘指数实时点位与涨跌幅（新浪优先，东财兜底）。"""
    try:
        r = _retry_get(
            "https://hq.sinajs.cn/list=" + ",".join(_INDEX_CODES.keys()),
            headers={**_UA, "Referer": "https://finance.sina.com.cn/"},
            encoding="gbk", timeout=8)
        out = {}
        for line in r.text.strip().split("\n"):
            if '"' not in line:
                continue
            tag = line.split("=")[0].split("_")[-1].strip()
            f = line.split('"')[1].split(",")
            if len(f) > 3 and f[2] and f[3]:
                prev, now = float(f[2]), float(f[3])
                out[_INDEX_CODES.get(tag, tag)] = {
                    "price": round(now, 2),
                    "change_pct": round((now / prev - 1) * 100, 2) if prev else 0.0,
                }
        if out:
            return out
    except Exception:
        pass

    # 东方财富兜底
    r = _retry_get(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        params={"fltt": 2, "invt": 2,
                "secids": ",".join(_EM_INDEX_SECIDS.keys()),
                "fields": "f2,f3,f4,f12,f14"})
    diff = ((r.json().get("data") or {}).get("diff")) or []
    out = {}
    for d in diff:
        name = _EM_INDEX_SECIDS.get(f'{d.get("f13")}.{d.get("f12")}')
        if not name:
            continue
        out[name] = {"price": round(float(d.get("f2", 0) or 0), 2),
                     "change_pct": round(float(d.get("f3", 0) or 0), 2)}
    return out


# ============================================================
#  2. 涨停池 / 跌停池（东财 → akshare 兜底）
# ============================================================
def _zt_cache_ttl() -> float:
    return 90 if is_trading_time() else 6 * 3600


def _zt_from_akshare(date: str) -> List[LimitUpStock]:
    """akshare 东财涨停池兜底（push2ex 接口改版时使用）。"""
    import akshare as ak
    df = ak.stock_zt_pool_em(date=date)
    if df is None or df.empty:
        return []
    cols = {str(c): c for c in df.columns}
    out = []
    for _, row in df.iterrows():
        stat = str(row.get("涨停统计", "0/0"))
        try:
            boards = int(row.get("连板数", 0) or 0)
        except Exception:
            boards = 0
        out.append(LimitUpStock(
            code=str(row.get("代码", "")).zfill(6),
            name=str(row.get("名称", "")),
            price=float(row.get("最新价", 0) or 0),
            change_pct=round(float(row.get("涨跌幅", 0) or 0), 2),
            amount=float(row.get("成交额", 0) or 0),
            circ_mv=float(row.get("流通市值", 0) or 0),
            turnover=round(float(row.get("换手率", 0) or 0), 2),
            seal_fund=float(row.get("封板资金", 0) or 0),
            first_seal=_hhmmss(row.get("首次封板时间", 0)),
            last_seal=_hhmmss(row.get("最后封板时间", 0)),
            break_count=int(float(row.get("炸板次数", 0) or 0)),
            boards=boards, stat=stat,
            industry=str(row.get("所属行业", "") or ""),
        ))
    return out


def _fetch_zt_one(d: str) -> tuple:
    key = f"zt_pool_{d}"
    cached = cache_load(key, _zt_cache_ttl())
    if cached:
        return d, [LimitUpStock(**x) for x in cached]
    try:
        r = _retry_get(
            "https://push2ex.eastmoney.com/getTopicZTPool",
            params={"ut": _EM_UT, "dpt": "wz.ztzt", "Pageindex": 0,
                    "pagesize": 10000, "sort": "fbt:asc", "date": d},
            timeout=8, retries=1)
        pool = (r.json().get("data") or {}).get("pool") or []
        stocks = []
        for it in pool:
            stocks.append(LimitUpStock(
                code=it["c"], name=it["n"],
                price=it["p"] / 1000,
                change_pct=round(float(it.get("zdp", 0)), 2),
                amount=float(it.get("amount", 0)),
                circ_mv=float(it.get("ltsz", 0)),
                turnover=round(float(it.get("hs", 0)), 2),
                seal_fund=float(it.get("fund", 0)),
                first_seal=_hhmmss(it.get("fbt")),
                last_seal=_hhmmss(it.get("lbt")),
                break_count=int(it.get("zbc", 0)),
                boards=int(it.get("lbc", 0)),
                stat=f"{it['zttj']['days']}/{it['zttj']['ct']}",
                industry=it.get("hybk", "") or "",
            ))
        if stocks:
            cache_save(key, [asdict(s) for s in stocks])
            return d, stocks
    except Exception:
        pass
    # 兜底：akshare（同一个东财数据源的不同接口）
    try:
        stocks = _zt_from_akshare(d)
        if stocks:
            cache_save(key, [asdict(s) for s in stocks])
            return d, stocks
    except Exception:
        pass
    return d, []


def fetch_zt_pool(date: Optional[str] = None) -> tuple:
    """
    返回 (实际日期, [LimitUpStock])。
    并发尝试最近 3 个交易日，取最新的有数据的日期（避免串行 7 天空等）。
    """
    dates = [date] if date else recent_trade_dates(3)
    with ThreadPoolExecutor(max_workers=min(3, len(dates))) as ex:
        futs = [ex.submit(_fetch_zt_one, d) for d in dates]
        results = []
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:
                pass
    if date:
        for d, stocks in results:
            if d == date and stocks:
                return d, stocks
    for d, stocks in sorted(results, key=lambda x: x[0], reverse=True):
        if stocks:
            return d, stocks
    return (date or recent_trade_dates(1)[0]), []


def _dt_cache_ttl() -> float:
    return 120 if is_trading_time() else 6 * 3600


def fetch_dt_count(date: Optional[str] = None) -> int:
    """跌停家数（用于情绪温度），东财涨停板专题接口。"""
    dates = [date] if date else recent_trade_dates(3)

    def one(d):
        key = f"dt_count_{d}"
        cached = cache_load(key, _dt_cache_ttl())
        if cached is not None:
            return int(cached)
        r = _retry_get(
            "https://push2ex.eastmoney.com/getTopicDTPool",
            params={"ut": _EM_UT, "dpt": "wz.ztzt", "Pageindex": 0,
                    "pagesize": 10000, "sort": "fund:asc", "date": d},
            timeout=8, retries=1)
        pool = (r.json().get("data") or {}).get("pool") or []
        n = len(pool)
        cache_save(key, n)
        return n

    results = {}
    with ThreadPoolExecutor(max_workers=min(3, len(dates))) as ex:
        futs = {ex.submit(one, d): d for d in dates}
        for fut in as_completed(futs):
            try:
                results[futs[fut]] = int(fut.result())
            except Exception:
                continue
    # 按日期从新到旧，返回第一个成功结果（0 也是有效值）
    for d in dates:
        if d in results:
            return results[d]
    return 0


# ============================================================
#  3. 涨停原因：东财 F10 核心题材（7 天缓存）
# ============================================================
def fetch_core_themes(code: str) -> dict:
    """
    获取个股核心题材：{industry:[], concepts:[{name, reason}], region:""}。
    F10 题材变化很慢，磁盘缓存 7 天（大幅降低启动/荐股耗时）。
    """
    key = f"coretheme_{code}"
    cached = cache_load(key, 7 * 24 * 3600)
    if isinstance(cached, dict):
        return cached

    out = {"industry": [], "concepts": [], "region": ""}
    try:
        r = _retry_get(
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            params={"reportName": "RPT_F10_CORETHEME_BOARDTYPE",
                    "columns": "ALL",
                    "filter": f'(SECUCODE="{secucode(code)}")',
                    "pageNumber": 1, "pageSize": 100,
                    "sortColumns": "BOARD_TYPE", "sortTypes": "1",
                    "source": "HSF10", "client": "PC"},
            timeout=8, retries=0)
        data = (r.json().get("result") or {}).get("data") or []
        for row in data:
            bt, bn = row.get("BOARD_TYPE"), row.get("BOARD_NAME", "")
            reason = row.get("SELECTED_BOARD_REASON") or ""
            if bt == "板块":
                out["region"] = bn
            elif bt == "行业":
                out["industry"].append(bn)
            elif bn and not _is_noise_concept(bn):
                out["concepts"].append({"name": bn.strip(), "reason": reason})
    except Exception:
        pass

    if out["concepts"] or out["industry"]:
        cache_save(key, out)
    return out


def enrich_zt_reasons(stocks: List[LimitUpStock], workers: int = 8,
                      hot: Optional[set] = None, limit: int = 40) -> None:
    """批量为涨停股补充概念题材（就地修改）；只补最强的 limit 只，控制耗时。"""
    hot = hot or set()
    targets = sorted(
        stocks, key=lambda s: (s.boards, s.seal_fund), reverse=True)[:limit]

    def work(s: LimitUpStock):
        return s, fetch_core_themes(s.code)

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(targets)))) as ex:
        for fut in as_completed([ex.submit(work, s) for s in targets]):
            try:
                s, themes = fut.result()
            except Exception:
                continue
            concepts = [c["name"] for c in themes["concepts"]]
            s.concepts = concepts
            ranked = [c for c in concepts if c in hot] + \
                     [c for c in concepts if c not in hot]
            s.reasons = ranked[:3]
            reason_map = {c["name"]: c["reason"] for c in themes["concepts"]}
            for cname in s.reasons:
                detail = reason_map.get(cname, "")
                if detail:
                    s.reason_detail = f"{cname}：{detail}"
                    break


# ============================================================
#  4. 行业板块（新浪 + 东方财富）
# ============================================================
def fetch_industry_sectors() -> List[SectorInfo]:
    """新浪 49 个行业板块涨跌幅。"""
    cached = cache_load("sina_industry", 60 if is_trading_time() else 900)
    if cached:
        return [SectorInfo(**x) for x in cached]
    r = _retry_get(
        "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
        headers={**_UA, "Referer": "https://finance.sina.com.cn/"},
        encoding="gbk", timeout=8)
    text = r.text
    start = text.find("= {")
    if start < 0:
        return []
    data = json.loads(text[start + 2:text.rfind("}") + 1])
    out = []
    for _, info in data.items():
        p = info.split(",")
        if len(p) >= 13:
            try:
                out.append(SectorInfo(
                    name=p[1],
                    change_pct=round(float(p[5]), 2),
                    lead=p[12],
                    stock_count=int(p[2]),
                ))
            except Exception:
                continue
    out.sort(key=lambda x: x.change_pct, reverse=True)
    if out:
        cache_save("sina_industry", [asdict(x) for x in out])
    return out


def _fetch_em_boards(board_type: str, top: int = 100) -> List[dict]:
    """
    东财板块行情（按涨幅排序）。
      board_type='2' → 细分行业（约 100 个）
      board_type='3' → 概念板块（过滤统计标签后的真实概念）
    返回 [{code,name,change_pct,amount,lead}]，amount 为板块成交额（亿元）。
    """
    ttl = 60 if is_trading_time() else 900
    key = f"em_boards_{board_type}"
    cached = cache_load(key, ttl)
    if cached:
        return cached
    diff_all = []
    page = 1
    while len(diff_all) < top and page <= 3:
        r = _retry_get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={"pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2,
                    "invt": 2, "fid": "f3", "fs": f"m:90+t:{board_type}",
                    "fields": "f12,f14,f3,f6,f128"},
            timeout=8)
        page_diff = ((r.json().get("data") or {}).get("diff")) or []
        if not page_diff:
            break
        diff_all.extend(page_diff)
        page += 1
    out = []
    for d in diff_all[:top]:
        name = str(d.get("f14", ""))
        if not name:
            continue
        if board_type == "3" and _is_noise_concept(name):
            continue
        out.append({
            "code": str(d.get("f12", "")), "name": name,
            "change_pct": round(float(d.get("f3", 0) or 0), 2),
            "amount": round(float(d.get("f6", 0) or 0) / 1e8, 2),  # 亿元
            "lead": str(d.get("f128", "") or ""),
            "stock_count": 0,
        })
    if out:
        cache_save(key, out)
    return out


def fetch_em_industry_boards(top: int = 100) -> List[dict]:
    """东财细分行业板块（比新浪 49 行业更细，带领涨股）。"""
    return _fetch_em_boards("2", top)


def fetch_em_concept_boards(top: int = 100) -> List[dict]:
    """东财概念板块（当日涨幅靠前的真实概念，过滤统计/宽基标签）。"""
    return _fetch_em_boards("3", top)


# ============================================================
#  板块轮动引擎：资金流向 + 持续性 → 轮动强度与阶段
# ============================================================
def _rotation_stage(change_pct: float, momentum: float, amount: float) -> str:
    """按涨幅/动量/成交额粗判轮动阶段。"""
    if change_pct >= 2.5 and momentum > 0:
        return "升温启动" if amount > 0 else "加速"
    if change_pct >= 1.5:
        return "加速"
    if momentum <= -1:
        return "退潮"
    if change_pct >= 0.5:
        return "走强"
    return "平淡"


def fetch_sector_rotation(zt_today: Optional[List] = None,
                          ref: Optional[dt.date] = None) -> dict:
    """
    计算行业板块轮动强度。
    输入：今日涨停池（含 industry）；近 5 个交易日历史涨停（按行业聚类）。
    输出 {行业名: {change_pct, amount, zt_today, zt_avg5, momentum, stage}}。
    """
    today = {}
    for s in (zt_today or []):
        ind = (s.get("industry") or "").strip()
        if ind:
            today[ind] = today.get(ind, 0) + 1

    hist = {}
    try:
        days = fetch_zt_history(5, ref=ref)
        td = (zt_today[0].get("date") if zt_today else "") or \
            (ref or dt.date.today()).strftime("%Y%m%d")
        for rec in days:
            if rec["date"] == td:
                continue
            ind = (rec.get("industry") or "").strip()
            if not ind:
                continue
            hist.setdefault(ind, []).append(rec["date"])
    except Exception:
        pass

    boards = {b["name"]: b for b in fetch_em_industry_boards(120)}
    if not boards:
        return {}

    out = {}
    for ind, b in boards.items():
        amt = b.get("amount", 0)
        zt_t = today.get(ind, 0)
        zt_h = len(hist.get(ind, [])) if ind in hist else 0
        zt_avg5 = round(zt_h / 5.0, 2) if hist else 0.0
        momentum = zt_t - zt_avg5 if (zt_h or zt_t) else 0.0
        out[ind] = {
            "change_pct": b.get("change_pct", 0),
            "amount": amt,
            "zt_today": zt_t,
            "zt_avg5": zt_avg5,
            "momentum": round(momentum, 2),
            "lead": b.get("lead", ""),
            "stage": _rotation_stage(b.get("change_pct", 0), momentum, amt),
        }
    return out


# ============================================================
#  5. 财经新闻：多源直连（财联社 / 东财 / 同花顺 / 新浪）
# ============================================================
_CATEGORY_RULES = [
    ("政策政治", ["国务院", "中央", "政治局", "发改委", "证监会", "央行", "人民银行",
                "财政部", "工信部", "商务部", "政策", "发文", "规划", "监管", "法规",
                "关税", "出口管制", "反垄断", "补贴", "国常会", "部委", "两会", "规范",
                "方案", "意见", "通知", "改革", "国资委"]),
    ("宏观经济", ["GDP", "CPI", "PPI", "PMI", "利率", "降息", "加息", "降准", "社融",
                "M2", "通胀", "汇率", "美联储", "欧央行", "就业", "失业率", "进出口",
                "经济", "复苏", "衰退", "国债", "货币政策", "财政", "信贷", "MLF", "LPR",
                "逆回购", "外汇", "通缩", "滞胀"]),
    ("国际地缘", ["美国", "特朗普", "拜登", "俄罗斯", "乌克兰", "中东", "以色列", "伊朗",
                "欧盟", "日本", "韩国", "冲突", "战争", "北约", "OPEC", "原油", "地缘",
                "联合国", "朝鲜", "印度", "关税", "制裁"]),
    ("公司公告", ["公司", "公告", "净利润", "营收", "回购", "增持", "减持", "中标",
                "订单", "分红", "并购", "重组", "三季报", "年报", "业绩预告", "停牌",
                "复牌", "收购", "签约", "扭亏", "预增", "预减", "股权激励"]),
    ("网络舆情", ["热搜", "网传", "传闻", "网友", "舆情", "热议", "刷屏", "爆火",
                "爆款", "走红", "爆单", "排队", "抢购", "销量爆", "回应"]),
]

_BULL = ["利好", "增长", "回升", "降息", "降准", "增持", "回购", "突破", "中标", "订单",
         "补贴", "支持", "超预期", "大涨", "扭亏", "盈利", "签约", "落地", "放量",
         "创新高", "回暖", "提振", "减税", "复苏", "预增", "受益", "加码", "扩产",
         "获批", "上涨", "走高", "拉升", "飘红", "景气", "需求旺盛", "吃紧"]
_BEAR = ["利空", "下降", "下滑", "加息", "减持", "处罚", "亏损", "风险", "暴跌",
         "退市", "制裁", "管制", "违约", "爆雷", "减值", "计提", "诉讼", "停产",
         "召回", "下修", "预警", "滞胀", "衰退", "抛售", "预减", "跌停", "走低",
         "下挫", "跳水", "飘绿", "承压", "收紧", "立案", "警示", "ST", "炸雷"]

# 行业/概念关键词（用于把新闻关联到题材）
_CONCEPT_KW = ["机器人", "人工智能", "AI", "芯片", "半导体", "光伏", "锂电", "新能源",
               "汽车", "医药", "军工", "算力", "低空", "航天", "固态电池", "存储",
               "面板", "消费电子", "数据要素", "数字货币", "稀土", "有色", "煤炭",
               "银行", "券商", "保险", "地产", "白酒", "食品", "家电", "化工",
               "PCB", "铜箔", "光模块", "液冷", "华为", "苹果", "特斯拉", "英伟达",
               "人形机器人", "减速器", "智能驾驶", "飞行汽车", "eVTOL", "创投", "风电",
               "储能", "光伏", "氢能", "核电", "创新药", "中药", "医疗器械", "农业"]


def classify_news(title: str, summary: str) -> tuple:
    """返回 (category, sentiment, concepts)。"""
    text = f"{title} {summary}"
    category, best_hits = "行业产业", 0
    for cat, kws in _CATEGORY_RULES:
        hits = sum(1 for k in kws if k in text)
        if hits > best_hits:
            category, best_hits = cat, hits
    b = sum(text.count(w) for w in _BULL)
    d = sum(text.count(w) for w in _BEAR)
    sentiment = (b - d) / (b + d) if (b + d) > 0 else 0.0
    concepts = [k for k in _CONCEPT_KW if k in text]
    return category, round(sentiment, 2), concepts


def _mk_news(title: str, summary: str, tm: str, url: str,
             source: str) -> Optional[NewsItem]:
    title = (title or "").strip()
    if not title:
        title = (summary or "").strip()[:46]
    if not title:
        return None
    cat, sent, concepts = classify_news(title, summary)
    return NewsItem(title=title[:160], summary=(summary or "").strip()[:300],
                    time=tm, url=url, category=cat, sentiment=sent,
                    concepts=concepts, source=source)


def _news_cls(cap: int = 30) -> List[NewsItem]:
    """财联社电报（直连，带签名）。"""
    url = "https://www.cls.cn/v1/roll/get_roll_list"
    params = {"app": "CailianpressWeb", "category": "",
              "last_time": int(time.time()), "os": "web",
              "refresh_type": "1", "rn": str(cap), "sv": "8.4.6"}
    params["sign"] = hashlib.md5(
        hashlib.sha1(urlencode(params).encode("utf-8")).hexdigest()
        .encode("utf-8")).hexdigest()
    r = _retry_get(url, params=params, timeout=8, retries=0)
    rows = (r.json().get("data") or {}).get("roll_data") or []
    out = []
    for it in rows[:cap]:
        ctime = it.get("ctime")
        tm = dt.datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M") \
            if ctime else ""
        item = _mk_news(it.get("title", ""), it.get("content", ""), tm,
                        it.get("shareurl", "") or "", "财联社电报")
        if item:
            out.append(item)
    return out


def _news_em(cap: int = 60) -> List[NewsItem]:
    """东方财富·全球财经快讯（直连）。"""
    r = _retry_get(
        "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
        params={"client": "web", "biz": "web_724", "fastColumn": "102",
                "sortEnd": "", "pageSize": str(cap),
                "req_trace": str(int(time.time() * 1000))},
        timeout=8, retries=0)
    rows = (r.json().get("data") or {}).get("fastNewsList") or []
    out = []
    for it in rows[:cap]:
        code = it.get("code", "")
        link = f"https://finance.eastmoney.com/a/{code}.html" if code else ""
        item = _mk_news(it.get("title", ""), it.get("summary", ""),
                        it.get("showTime", ""), link, "东方财富快讯")
        if item:
            out.append(item)
    return out


def _news_ths(cap: int = 30) -> List[NewsItem]:
    """同花顺·全球财经直播（直连）。"""
    r = _retry_get("https://news.10jqka.com.cn/tapp/news/push/stock",
                   params={"page": "1", "tag": "", "track": "website"},
                   timeout=8, retries=0)
    rows = (r.json().get("data") or {}).get("list") or []
    out = []
    for it in rows[:cap]:
        rtime = it.get("rtime")
        tm = dt.datetime.fromtimestamp(int(rtime)).strftime("%Y-%m-%d %H:%M") \
            if rtime else ""
        item = _mk_news(it.get("title", ""), it.get("digest", ""), tm,
                        it.get("url", "") or "", "同花顺快讯")
        if item:
            out.append(item)
    return out


def _news_sina(cap: int = 20) -> List[NewsItem]:
    """新浪财经·7×24 全球快讯（直连）。"""
    r = _retry_get("https://zhibo.sina.com.cn/api/zhibo/feed",
                   params={"page": "1", "page_size": str(cap),
                           "zhibo_id": "152", "tag_id": "0", "dire": "f",
                           "dpc": "1", "pagesize": str(cap), "type": "1"},
                   timeout=8, retries=0)
    rows = (((r.json().get("result") or {}).get("data") or {})
            .get("feed") or {}).get("list") or []
    out = []
    for it in rows[:cap]:
        item = _mk_news("", it.get("rich_text", ""),
                        it.get("create_time", ""), "", "新浪快讯")
        if item:
            out.append(item)
    return out


def _akshare_news(cap: int = 30) -> List[NewsItem]:
    """akshare 兜底（仅直连全部失败时使用，首次 import 较慢）。"""
    import akshare as ak
    df = ak.stock_info_global_cls()
    if df is None or df.empty:
        return []
    out = []
    for _, row in df.head(cap).iterrows():
        tm = f"{row.get('发布日期', '')} {row.get('发布时间', '')}".strip()
        item = _mk_news(str(row.get("标题", "")), str(row.get("内容", "")),
                        tm, "", "财联社电报(兜底)")
        if item:
            out.append(item)
    return out


def fetch_all_news(per_source: int = 50, force: bool = False) -> tuple:
    """
    合并四大新闻源（各自独立 try，单源失败不影响其他）：
      财联社电报 / 东方财富快讯 / 同花顺快讯 / 新浪快讯。
    返回 (NewsItem 列表, 实际成功的来源名列表)。
    """
    if not force:
        cached = cache_load("news_merged", 90 if is_trading_time() else 600)
        if cached:
            items = [NewsItem(**x) for x in cached.get("items", [])]
            return items, cached.get("sources", [])

    loaders = [("财联社电报", _news_cls), ("东方财富快讯", _news_em),
               ("同花顺快讯", _news_ths), ("新浪快讯", _news_sina)]
    items, sources = [], []
    with ThreadPoolExecutor(max_workers=len(loaders)) as ex:
        futs = {ex.submit(fn, per_source): name for name, fn in loaders}
        for fut in as_completed(futs, timeout=12):
            name = futs[fut]
            try:
                got = fut.result()
            except Exception:
                continue
            if got:
                items += got
                sources.append(name)

    if not items:
        try:
            items = _akshare_news(per_source)
            if items:
                sources.append("财联社电报(兜底)")
        except Exception:
            pass

    seen, uniq = set(), []
    for it in items:
        key = it.title[:40]
        if key and key not in seen:
            seen.add(key)
            uniq.append(it)
    if uniq:
        cache_save("news_merged", {"items": [asdict(x) for x in uniq],
                                   "sources": sources})
    return uniq, sources


# 兼容旧调用
_CONCEPT_KWS = _CONCEPT_KW


def fetch_news(per_source: int = 50) -> List[NewsItem]:
    return fetch_all_news(per_source)[0]


# ============================================================
#  6. 热点概念聚类 + 市场情绪温度
# ============================================================
def aggregate_hot_concepts(stocks: List[LimitUpStock], top: int = 20) -> List[dict]:
    """对涨停股概念做频次统计，返回 [{concept, count, stocks:[name]}]。"""
    tally: dict = {}
    for s in stocks:
        for c in s.concepts:
            entry = tally.setdefault(c, {"concept": c, "count": 0, "stocks": []})
            entry["count"] += 1
            entry["stocks"].append(s.name)
    res = sorted(tally.values(), key=lambda x: x["count"], reverse=True)
    for e in res:
        e["stocks"] = e["stocks"][:6]
    return res[:top]


def sentiment_temperature(stocks: List[LimitUpStock], dt_count: int,
                          indices: dict) -> dict:
    """市场情绪温度（0~100）与关键指标。"""
    zt = len(stocks)
    breaks = sum(s.break_count for s in stocks)
    zha_ban_rate = round(breaks / (zt + breaks) * 100, 1) if (zt + breaks) else 0.0
    max_boards = max((s.boards for s in stocks), default=0)
    lianban = sum(1 for s in stocks if s.boards >= 2)
    idx_pcts = [v["change_pct"] for v in indices.values()]
    idx_avg = round(sum(idx_pcts) / len(idx_pcts), 2) if idx_pcts else 0.0

    score = 0.0
    score += min(zt / 80, 1) * 35
    score += min(max_boards / 6, 1) * 20
    score += (1 - zha_ban_rate / 100) * 20
    score += min(max(idx_avg, -3) / 3 * 0.5 + 0.5, 1) * 25
    temp = round(score, 1)

    if temp >= 65:
        label = "亢奋/过热"
    elif temp >= 50:
        label = "偏多"
    elif temp >= 35:
        label = "中性偏弱"
    else:
        label = "低迷/冰点"
    return {
        "temperature": temp, "label": label,
        "zt_count": zt, "dt_count": dt_count,
        "zha_ban_rate": zha_ban_rate,
        "max_boards": max_boards, "lianban": lianban,
        "index_avg_pct": idx_avg,
    }


# ============================================================
#  资金面：龙虎榜 + 北向资金（东财数据中心公开报表）
# ============================================================
_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _dc_rows(report: str, **params) -> list:
    """东财数据中心通用查询；调用方负责降级处理。"""
    p = {"reportName": report, "columns": "ALL", "source": "WEB",
         "client": "WEB", "pageSize": "50", "pageNumber": "1"}
    p.update({k: v for k, v in params.items() if v is not None})
    r = _retry_get(_DATACENTER, params=p, timeout=10, retries=1)
    j = r.json()
    if j.get("success") is False:
        raise RuntimeError(str(j.get("message") or "数据中心返回失败"))
    return ((j.get("result") or {}).get("data")) or []


def _fnum(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _dash(d: str) -> str:
    """YYYYMMDD → YYYY-MM-DD（已是带横线格式则原样返回）。"""
    d = str(d or "")
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def fetch_lhb(date: Optional[str] = None, limit: int = 40) -> dict:
    """龙虎榜：按净买额降序，同一只票的多个上榜原因合并。

    东财报表为收盘后更新，当天未出数据时自动回退到前一个交易日。
    """
    candidates = [_dash(date)] if date else \
        [_dash(x) for x in recent_trade_dates(3)]
    rows, used = [], ""
    for d in candidates:
        try:
            rows = _dc_rows("RPT_DAILYBILLBOARD_DETAILSNEW",
                            sortColumns="BILLBOARD_NET_AMT,SECURITY_CODE",
                            sortTypes="-1,1",
                            filter=f"(TRADE_DATE='{d}')")
        except Exception:
            rows = []
        if rows:
            used = d
            break
    merged = {}
    for x in rows:
        code = str(x.get("SECURITY_CODE") or "")
        if not code:
            continue
        buy = _fnum(x.get("BILLBOARD_BUY_AMT"))
        sell = _fnum(x.get("BILLBOARD_SELL_AMT"))
        reason = str(x.get("EXPLANATION") or "").strip()
        if code in merged:
            if reason and reason not in merged[code]["reasons"]:
                merged[code]["reasons"].append(reason)
            continue
        merged[code] = {
            "code": code,
            "name": x.get("SECURITY_NAME_ABBR") or "",
            "price": _fnum(x.get("CLOSE_PRICE")),
            "change_pct": _fnum(x.get("CHANGE_RATE")),
            "net_buy": _fnum(x.get("BILLBOARD_NET_AMT")) or (buy - sell),
            "buy_amt": buy,
            "sell_amt": sell,
            "amount": _fnum(x.get("BILLBOARD_DEAL_AMT")),
            "turnover": _fnum(x.get("TURNOVERRATE")),
            "reasons": [reason] if reason else [],
            "date": used,
        }
    items = sorted(merged.values(), key=lambda v: v["net_buy"], reverse=True)
    return {"date": used, "rows": items[:limit], "count": len(items)}


def fetch_north_market(date: str) -> dict:
    """单日北向（沪股通+深股通）成交额，单位元。

    东财 RPT_MUTUAL_DEAL_HISTORY 的 DEAL_AMT 以百万元计，这里换算成元。
    MUTUAL_TYPE：001 沪股通、003 深股通、005 北向合计。
    """
    d = _dash(date)
    try:
        rows = _dc_rows("RPT_MUTUAL_DEAL_HISTORY",
                        filter=f"(TRADE_DATE='{d}')",
                        sortColumns="MUTUAL_TYPE", sortTypes="1")
    except Exception:
        rows = []
    m = {str(x.get("MUTUAL_TYPE") or ""): _fnum(x.get("DEAL_AMT"))
         for x in rows}
    sh = m.get("001", 0.0) * 1e6
    sz = m.get("003", 0.0) * 1e6
    total = m.get("005", 0.0) * 1e6 or (sh + sz)
    return {"date": d, "sh": sh, "sz": sz, "total": total}


def fetch_northbound(date: Optional[str] = None, limit: int = 20) -> dict:
    """北向资金：每日净买额自 2024-08-19 起停止披露。

    仍可用的口径是「北向十大成交股」的成交额，按成交额降序展示；
    净买额字段保留但通常为空，界面统一显示为“暂停披露”。
    """
    candidates = [_dash(date)] if date else \
        [_dash(x) for x in recent_trade_dates(3)]
    rows, used = [], ""
    for d in candidates:
        try:
            rows = _dc_rows("RPT_MUTUAL_TOP10DEAL",
                            filter=f"(TRADE_DATE='{d}')",
                            sortColumns="DEAL_AMT", sortTypes="-1")
        except Exception:
            rows = []
        if rows:
            used = d
            break
    out = []
    for x in rows:
        mt = str(x.get("MUTUAL_TYPE") or "")
        if mt not in ("001", "003"):  # 001 沪股通 / 003 深股通
            continue
        out.append({
            "market": "沪股通" if mt == "001" else "深股通",
            "code": str(x.get("SECURITY_CODE") or ""),
            "name": x.get("SECURITY_NAME") or "",
            "change_pct": _fnum(x.get("CHANGE_RATE")),
            "deal_amt": _fnum(x.get("DEAL_AMT")),
            "net_buy": _fnum(x.get("NET_BUY_AMT")),
            "rank": int(_fnum(x.get("RANK")) or 0),
        })
    out.sort(key=lambda v: v["deal_amt"], reverse=True)
    market = fetch_north_market(used) if used else {}
    return {"date": used, "rows": out[:limit], "market": market,
            "note": "北向每日净买额自 2024-08-19 起停止披露，"
                    "本表按十大成交股成交额排序"}


# ============================================================
#  资金面区间数据（供回测/因子使用，按日期缓存）
# ============================================================
_FUND_MEM: dict = {}
_PAGE_SIZE = 500  # 东财数据中心单页上限


def _dc_page(report: str, page: int, flt: str, sort_cols: str,
             sort_types: str, page_size: int = _PAGE_SIZE) -> tuple:
    """取一页数据，返回 (rows, total_pages)。"""
    p = {"reportName": report, "columns": "ALL", "source": "WEB",
         "client": "WEB", "pageSize": str(page_size),
         "pageNumber": str(page), "sortColumns": sort_cols,
         "sortTypes": sort_types}
    if flt:
        p["filter"] = flt
    r = _retry_get(_DATACENTER, params=p, timeout=15, retries=1)
    j = r.json()
    if j.get("success") is False:
        raise RuntimeError(str(j.get("message") or "数据中心返回失败"))
    res = j.get("result") or {}
    return (res.get("data") or []), int(res.get("pages") or 0)


def _dc_all_pages(report: str, flt: str, sort_cols: str, sort_types: str,
                  max_pages: int = 90, workers: int = 6) -> list:
    """并发抓完所有分页。"""
    rows, pages = _dc_page(report, 1, flt, sort_cols, sort_types)
    total = list(rows)
    pages = min(pages, max_pages)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_dc_page, report, pg, flt, sort_cols,
                              sort_types) for pg in range(2, pages + 1)]
            for fu in futs:
                try:
                    total.extend(fu.result()[0])
                except Exception:
                    continue
    return total


def fetch_lhb_range(start: str, end: str) -> dict:
    """抓取区间内龙虎榜，返回 {日期: {代码: 净买额}}。

    同一只票同一天可能有多条上榜原因，取其中净买额最大的一条。
    """
    flt = f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"
    rows = _dc_all_pages("RPT_DAILYBILLBOARD_DETAILSNEW", flt,
                         "TRADE_DATE,SECURITY_CODE", "1,1")
    out: dict = {}
    for x in rows:
        d = str(x.get("TRADE_DATE") or "")[:10]
        code = str(x.get("SECURITY_CODE") or "")
        if not d or not code:
            continue
        buy = _fnum(x.get("BILLBOARD_BUY_AMT"))
        sell = _fnum(x.get("BILLBOARD_SELL_AMT"))
        net = _fnum(x.get("BILLBOARD_NET_AMT")) or (buy - sell)
        slot = out.setdefault(d, {})
        if net > slot.get(code, -1e18):
            slot[code] = net
    return out


def fetch_north_range(start: str, end: str) -> dict:
    """抓取区间内北向十大成交股成交额，返回 {日期: {代码: 成交额}}。"""
    flt = f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"
    rows = _dc_all_pages("RPT_MUTUAL_TOP10DEAL", flt,
                         "TRADE_DATE,MUTUAL_TYPE,RANK", "1,1,1")
    out: dict = {}
    for x in rows:
        # 001 沪股通 / 003 深股通（北向）；002/004 为南向，剔除
        if str(x.get("MUTUAL_TYPE") or "") not in ("001", "003"):
            continue
        d = str(x.get("TRADE_DATE") or "")[:10]
        code = str(x.get("SECURITY_CODE") or "")
        if not d or len(code) != 6:
            continue
        amt = _fnum(x.get("DEAL_AMT"))
        slot = out.setdefault(d, {})
        slot[code] = slot.get(code, 0.0) + amt
    return out


def fund_flow_range(start: str, end: str,
                    progress: Optional[Callable[[str], None]] = None,
                    ttl_hours: float = 6.0) -> dict:
    """区间龙虎榜 + 北向成交额，带内存与磁盘缓存。

    返回 {"lhb": {日期: {代码: 净买额}}, "north": {日期: {代码: 成交额}},
          "ok": {"lhb": bool, "north": bool}, "start", "end"}
    """
    log = progress or (lambda s: None)
    key = f"{start}_{end}"
    if key in _FUND_MEM:
        return _FUND_MEM[key]
    path = os.path.join(CACHE_DIR, f"fund_flow_{start}_{end}.json")
    data = None
    if os.path.exists(path):
        try:
            if time.time() - os.path.getmtime(path) < ttl_hours * 3600:
                with open(path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
        except Exception:
            data = None
    if data is None:
        log("抓取龙虎榜区间数据…")
        try:
            lhb = fetch_lhb_range(start, end)
            ok_lhb = True
        except Exception:
            lhb, ok_lhb = {}, False
        log("抓取北向十大成交股区间数据…")
        try:
            north = fetch_north_range(start, end)
            ok_north = True
        except Exception:
            north, ok_north = {}, False
        data = {"lhb": lhb, "north": north,
                "ok": {"lhb": ok_lhb, "north": ok_north},
                "start": start, "end": end}
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False)
        except Exception:
            pass
    _FUND_MEM[key] = data
    return data


# ============================================================
#  龙虎榜席位（游资）画像
# ============================================================
_SEAT_QUAL_FILE = os.path.join(CACHE_DIR, "seat_quality.json")
_ALIAS_FILE = os.path.join(CACHE_DIR, "seat_alias.json")
_SEAT_MEM: dict = {}

# 常见席位别名（社区叫法，仅供参考，可在 seat_alias.json 自行修改）
_DEFAULT_ALIAS = {
    "华鑫证券有限责任公司上海分公司": "华鑫上海（游资/量化混合）",
    "东方财富证券股份有限公司拉萨团结路第二证券营业部": "拉萨天团（散户集中营）",
    "东方财富证券股份有限公司拉萨团结路第一证券营业部": "拉萨天团（散户集中营）",
    "东方财富证券股份有限公司拉萨东环路第二证券营业部": "拉萨天团（散户集中营）",
    "东方财富证券股份有限公司拉萨东环路第一证券营业部": "拉萨天团（散户集中营）",
    "东方财富证券股份有限公司拉萨金融城南环路证券营业部": "拉萨天团（散户集中营）",
    "东方财富证券股份有限公司拉萨北京中路证券营业部": "拉萨天团（散户集中营）",
    "中国银河证券股份有限公司绍兴证券营业部": "绍兴帮",
    "国盛证券有限责任公司宁波桑田路证券营业部": "宁波桑田路",
    "华泰证券股份有限公司厦门厦禾路证券营业部": "厦门厦禾路",
    "华泰证券股份有限公司深圳益田路荣超商务中心证券营业部": "深圳益田路",
    "中信证券股份有限公司上海溧阳路证券营业部": "溧阳路",
    "中信证券股份有限公司上海分公司": "中信上海（机构/游资混合）",
    "财通证券股份有限公司杭州上塘路证券营业部": "杭州上塘路",
    "光大证券股份有限公司佛山绿景路证券营业部": "佛山系",
    "国泰君安证券股份有限公司南京太平南路证券营业部": "南京太平南路",
    "国泰君安证券股份有限公司上海江苏路证券营业部": "上海江苏路",
    "招商证券股份有限公司深圳蛇口工业七路证券营业部": "蛇口系",
    "高盛(中国)证券有限责任公司上海浦东新区世纪大道证券营业部": "外资（量化）",
    "瑞银证券有限责任公司上海花园石桥路证券营业部": "外资（量化）",
    "摩根大通证券(中国)有限公司上海银城中路证券营业部": "外资（量化）",
    "摩根士丹利证券(中国)有限公司上海环贸广场证券营业部": "外资（量化）",
    "机构专用": "机构专用",
}


def _norm_seat(name: str) -> str:
    """席位名归一化：去掉公司后缀与空格，便于跨报表匹配。"""
    s = str(name or "")
    for token in ("股份有限公司", "有限责任公司", "有限公司", "证券营业部", "营业部", " ", "　"):
        s = s.replace(token, "")
    return s


def seat_alias_of(name: str, alias: Optional[dict] = None) -> str:
    """按别名表解析席位昵称；支持全称/简称、后缀差异的模糊匹配。"""
    table = alias if alias is not None else load_seat_alias()
    if not table:
        return ""
    hit = table.get(name)
    if hit:
        return hit
    nk = _norm_seat(name)
    for k, v in table.items():
        kn = _norm_seat(k)
        if not kn:
            continue
        if kn == nk or (len(kn) >= 6 and (kn in nk or nk in kn)):
            return v
    return ""


def load_seat_alias() -> dict:
    """加载席位别名映射；不存在时写入默认表，并把新增默认项合并进已有文件。"""
    data = {}
    try:
        if os.path.exists(_ALIAS_FILE):
            with open(_ALIAS_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                data = {}
    except Exception:
        data = {}
    merged = dict(_DEFAULT_ALIAS)
    merged.update(data)  # 用户已有配置优先
    if merged != data:
        try:
            with open(_ALIAS_FILE, "w", encoding="utf-8") as fp:
                json.dump(merged, fp, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return merged


def _seat_quality(prob, samples, min_n: int = 5) -> float:
    """席位质量 0..1：用历史“上榜后3日上涨概率”映射，样本不足给中性 0.5。"""
    try:
        p = float(prob)
        n = int(samples or 0)
    except Exception:
        return 0.5
    if n < min_n:
        return 0.5
    return max(0.0, min(1.0, (p - 25.0) / 50.0))


def fetch_lhb_seats(date: Optional[str] = None,
                    max_pages: int = 4) -> dict:
    """某交易日龙虎榜席位明细，返回 {代码: {buys, sells, quality}}。

    quality ∈ [-1, 1]：买入席位平均质量 − 卖出席位平均质量（按金额加权）。
    """
    d = _dash(date) if date else fetch_lhb()["date"]
    if d in _SEAT_MEM:
        return _SEAT_MEM[d]
    out: dict = {}
    try:
        buys = _dc_all_pages("RPT_BILLBOARD_DAILYDETAILSBUY",
                             f"(TRADE_DATE='{d}')",
                             "SECURITY_CODE,BUY", "-1,-1",
                             max_pages=max_pages)
        sells = _dc_all_pages("RPT_BILLBOARD_DAILYDETAILSSELL",
                              f"(TRADE_DATE='{d}')",
                              "SECURITY_CODE,SELL", "-1,-1",
                              max_pages=max_pages)
    except Exception:
        buys, sells = [], []

    def _add(rows, side):
        for x in rows:
            code = str(x.get("SECURITY_CODE") or "")
            if not code:
                continue
            slot = out.setdefault(code, {"buys": [], "sells": []})
            slot[side].append({
                "seat": str(x.get("OPERATEDEPT_NAME") or ""),
                "code": str(x.get("OPERATEDEPT_CODE") or ""),
                "amount": _fnum(x.get("BUY") if side == "buys"
                                else x.get("SELL")),
                "net": _fnum(x.get("NET")),
                "reason": str(x.get("EXPLANATION") or ""),
                "prob3": x.get("RISE_PROBABILITY_3DAY"),
                "samples": x.get("TOTAL_BUYER_SALESTIMES_3DAY"),
            })

    _add(buys, "buys")
    _add(sells, "sells")

    for code, slot in out.items():
        bw = sum(b["amount"] for b in slot["buys"])
        sw = sum(s["amount"] for s in slot["sells"])
        bq = sum(b["amount"] * _seat_quality(b["prob3"], b["samples"])
                 for b in slot["buys"]) / bw if bw > 0 else 0.5
        sq = sum(s["amount"] * _seat_quality(s["prob3"], s["samples"])
                 for s in slot["sells"]) / sw if sw > 0 else 0.5
        slot["quality"] = round(float(bq - sq), 3)
        for side in ("buys", "sells"):
            slot[side].sort(key=lambda v: v["amount"], reverse=True)
            slot[side] = slot[side][:5]
    _SEAT_MEM[d] = out
    return out


def seat_quality_range(dates: list, progress: Optional[Callable[[str], None]] = None,
                       workers: int = 6) -> dict:
    """按交易日并发抓取席位质量，返回 {日期: {代码: quality}}，带磁盘缓存。"""
    log = progress or (lambda s: None)
    cache = {}
    try:
        if os.path.exists(_SEAT_QUAL_FILE):
            with open(_SEAT_QUAL_FILE, "r", encoding="utf-8") as fp:
                cache = json.load(fp)
    except Exception:
        cache = {}
    todo = [d for d in dict.fromkeys(dates) if d not in cache]
    if todo:
        log(f"抓取 {len(todo)} 个交易日的龙虎榜席位…")

        def _one(day):
            try:
                seats = fetch_lhb_seats(day)
                return day, {c: float(v.get("quality", 0.0))
                             for c, v in seats.items()}
            except Exception:
                return day, {}

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for day, val in ex.map(_one, todo):
                cache[day] = val
        try:
            with open(_SEAT_QUAL_FILE, "w", encoding="utf-8") as fp:
                json.dump(cache, fp, ensure_ascii=False)
        except Exception:
            pass
    return {d: cache.get(d, {}) for d in dates}


def fetch_seat_rank(limit: int = 200, cycle: str = "04") -> list:
    """营业部（游资席位）排行：上榜次数 + 上榜后 1/3/5/10 日平均涨幅与胜率。"""
    rows = _dc_all_pages("RPT_RATEDEPT_RETURNT_RANKING", "",
                         "TOTAL_BUYER_SALESTIMES_5DAY", "-1",
                         max_pages=12)
    picked = [x for x in rows
              if str(x.get("STATISTICSCYCLE") or "") == cycle] or rows
    out = []
    alias = load_seat_alias()
    for x in picked:
        name = str(x.get("OPERATEDEPT_NAME") or "")
        if "营业部" not in name and name != "机构专用":
            continue  # 过滤沪深股通专用/合并口径等非营业部席位
        out.append({
            "seat": name,
            "alias": seat_alias_of(name, alias),
            "code": str(x.get("OPERATEDEPT_CODE") or ""),
            "n1": int(_fnum(x.get("TOTAL_BUYER_SALESTIMES_1DAY"))),
            "n5": int(_fnum(x.get("TOTAL_BUYER_SALESTIMES_5DAY"))),
            "avg1": round(_fnum(x.get("AVERAGE_INCREASE_1DAY")), 2),
            "avg3": round(_fnum(x.get("AVERAGE_INCREASE_3DAY")), 2),
            "avg5": round(_fnum(x.get("AVERAGE_INCREASE_5DAY")), 2),
            "avg10": round(_fnum(x.get("AVERAGE_INCREASE_10DAY")), 2),
            "prob5": round(_fnum(x.get("RISE_PROBABILITY_5DAY")), 1),
        })
    out.sort(key=lambda v: (v["n5"], v["avg5"]), reverse=True)
    return out[:limit]


def refresh_all_stream(progress: Optional[Callable[[str], None]] = None,
                       date: Optional[str] = None,
                       emit: Optional[Callable[[str, object], None]] = None,
                       force: bool = False, budget: float = 30.0):
    """
    抓取并返回完整市场快照，同时通过 emit(stage, payload) 流式回调：
      indices / limit_up / sectors / boards / news / hot / rotation /
      lhb / north
    界面先展示缓存，再按区块逐个填充；总预算 budget 秒，超时任务跳过。
    """
    def log(msg):
        if progress:
            progress(msg)

    def push(stage, payload):
        if emit:
            try:
                emit(stage, payload)
            except Exception:
                pass

    t0 = time.time()

    def remain() -> float:
        return max(0.5, budget - (time.time() - t0))

    errors: List[str] = []
    asof = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snap = MarketSnapshot(asof=asof,
                          trade_date=date or recent_trade_dates(1)[0])
    log("正在并发抓取市场信息（指数/涨停/板块/新闻）…")

    # ---------- 第一波：7 个独立任务并发 ----------
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            ex.submit(fetch_indices): "indices",
            ex.submit(fetch_zt_pool, date): "zt",
            ex.submit(fetch_dt_count, None): "dt",
            ex.submit(fetch_industry_sectors): "sectors",
            ex.submit(fetch_em_industry_boards): "em_ind",
            ex.submit(fetch_em_concept_boards): "em_con",
            ex.submit(fetch_all_news, 50, force): "news",
            ex.submit(fetch_lhb, date): "lhb",
            ex.submit(fetch_northbound, date): "north",
        }
        pending = set(futs.keys())
        try:
            for fut in as_completed(pending, timeout=remain()):
                pending.discard(fut)
                stage = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa
                    errors.append(f"{stage}获取失败: {e}")
                    continue
                if stage == "indices":
                    snap.indices = res or {}
                    snap.sources["indices"] = "新浪/东财"
                    push("indices", {"indices": snap.indices})
                elif stage == "zt":
                    snap.trade_date, zt = res
                    snap.limit_up = [dict(asdict(s), date=snap.trade_date)
                                     for s in zt]
                    push("limit_up", {"trade_date": snap.trade_date,
                                      "limit_up": snap.limit_up})
                elif stage == "dt":
                    snap.limit_down_count = int(res or 0)
                elif stage == "sectors":
                    snap.sectors = [asdict(x) for x in (res or [])]
                    snap.sources["sectors"] = "新浪行业"
                    push("sectors", {"sectors": snap.sectors})
                elif stage == "em_ind":
                    snap.em_industry_boards = res or []
                    snap.sources["industry_boards"] = "东方财富"
                elif stage == "em_con":
                    snap.em_concept_boards = res or []
                    push("boards", {
                        "em_industry_boards": snap.em_industry_boards,
                        "em_concept_boards": snap.em_concept_boards})
                elif stage == "news":
                    items, srcs = res
                    snap.news = [asdict(x) for x in items]
                    snap.news_sources = srcs
                    push("news", {"news": snap.news,
                                  "news_sources": snap.news_sources})
                elif stage == "lhb":
                    snap.lhb = res.get("rows") or []
                    snap.sources["lhb"] = "东方财富龙虎榜"
                    push("lhb", {"lhb": snap.lhb,
                                 "date": res.get("date", "")})
                elif stage == "north":
                    snap.northbound = res or {}
                    snap.sources["northbound"] = "东方财富沪深港通"
                    push("north", {"northbound": snap.northbound})
                snap.sentiment = sentiment_temperature(
                    [_zt_obj(x) for x in snap.limit_up],
                    snap.limit_down_count, snap.indices)
                push("sentiment", {"sentiment": snap.sentiment})
        except TimeoutError:
            errors.append("部分信息源响应超时，已使用可用数据继续")
            for fut in pending:
                fut.cancel()

    # ---------- 第二波：依赖涨停池的题材补全 + 板块轮动 ----------
    zt_objs = [_zt_obj(x) for x in snap.limit_up]
    if zt_objs:
        log("正在补全涨停原因与板块轮动…")
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_theme = ex.submit(enrich_zt_reasons, zt_objs, 6, set(), 35)
            f_rot = ex.submit(fetch_sector_rotation, snap.limit_up)
            try:
                f_theme.result(timeout=min(14, remain()))
            except Exception as e:  # noqa
                errors.append(f"涨停原因补全超时/失败: {e}")
            try:
                rotation = f_rot.result(timeout=min(10, remain()))
                snap.rotation = [
                    dict(name=name, **info)
                    for name, info in (rotation or {}).items()]
                push("rotation", {"rotation": snap.rotation})
            except Exception as e:  # noqa
                errors.append(f"板块轮动获取失败: {e}")

    snap.limit_up = [asdict(s) for s in zt_objs] if zt_objs else snap.limit_up
    snap.hot_concepts = aggregate_hot_concepts(zt_objs)
    hot_set = {h["concept"] for h in snap.hot_concepts[:10]}
    if zt_objs:
        for s in zt_objs:
            if s.concepts:
                ranked = [c for c in s.concepts if c in hot_set] + \
                         [c for c in s.concepts if c not in hot_set]
                if ranked and ranked != s.reasons:
                    s.reasons = ranked[:3]
        snap.limit_up = [asdict(s) for s in zt_objs]
    snap.sentiment = sentiment_temperature(
        zt_objs, snap.limit_down_count, snap.indices)
    snap.errors = errors
    push("hot", {"hot_concepts": snap.hot_concepts,
                 "limit_up": snap.limit_up,
                 "sentiment": snap.sentiment})
    snap.asof = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"信息刷新完成（{len(snap.limit_up)} 只涨停，{len(snap.news)} 条新闻）")
    return snap


def refresh_all(progress: Optional[Callable[[str], None]] = None,
                date: Optional[str] = None, force: bool = False,
                emit: Optional[Callable[[str, object], None]] = None):
    """兼容旧调用：无 emit 时等价于一次性返回完整快照。"""
    return refresh_all_stream(progress=progress, date=date, emit=emit,
                              force=force)


# ============================================================
#  最新快照的本地持久化（启动先显缓存、再后台覆盖）
# ============================================================
def save_latest_snapshot(snap: MarketSnapshot) -> None:
    """原子写入最新快照：先写临时文件再替换，避免写一半损坏。"""
    tmp = LATEST_SNAPSHOT + ".tmp"
    snap.to_json(tmp)
    os.replace(tmp, LATEST_SNAPSHOT)


def load_latest_snapshot() -> Optional[MarketSnapshot]:
    """读取上次完整快照；不存在或损坏时返回 None。"""
    if not os.path.exists(LATEST_SNAPSHOT):
        return None
    try:
        with open(LATEST_SNAPSHOT, "r", encoding="utf-8") as f:
            d = json.load(f)
        snap = MarketSnapshot.from_dict(d)
        snap.from_cache = True
        return snap
    except Exception:
        return None


# ============================================================
#  历史涨停（供荐股/复盘）
# ============================================================
def fetch_zt_history(days: int = 20, ref: Optional[dt.date] = None) -> List[dict]:
    """
    抓取近 days 个交易日的涨停记录（含日期）。
    并发按日拉取（带当日缓存），不再串行等待，5 日历史约 1~2 秒。
    """
    dates = recent_trade_dates(days + 3, ref=ref)
    out = []

    def one(d):
        _, zt = _fetch_zt_one(d)
        return d, [{"date": d, "code": s.code, "name": s.name,
                    "industry": s.industry, "boards": s.boards} for s in zt]

    with ThreadPoolExecutor(max_workers=min(6, len(dates))) as ex:
        futs = [ex.submit(one, d) for d in dates]
        for fut in as_completed(futs):
            try:
                d, recs = fut.result()
            except Exception:
                continue
            out.extend(recs)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


if __name__ == "__main__":
    snap = refresh_all(progress=lambda m: print(m))
    print("\n==> 快照：")
    print("trade_date:", snap.trade_date)
    print("指数:", snap.indices)
    print("涨停:", len(snap.limit_up), " 跌停:", snap.limit_down_count)
    print("热点概念:", [(h["concept"], h["count"]) for h in snap.hot_concepts[:8]])
    print("情绪:", snap.sentiment)
    print("新闻条数:", len(snap.news), snap.news_sources)
    for s in snap.limit_up[:5]:
        print(f"  {s['code']} {s['name']} | {s['industry']} | 原因:{s['reasons']}")
    if snap.errors:
        print("errors:", snap.errors)
