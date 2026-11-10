# -*- coding: utf-8 -*-
"""
多数据源接入层
==============
内置免费源：akshare / baostock / 东方财富 / 腾讯 / 新浪
券商标配源（需凭证）：
  - 湘财证券：Tushare 合作平台 (tushare.xcsc.com)，需 Tushare token
  - 中国银河：格物金融数据 SDK (mdga)，需银河账号凭证

所有数据源统一输出字段：date, open, high, low, close, volume, amount
调用方无需关心底层差异，自动回退。
"""

import datetime as _dt_base
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import pandas as pd
import requests

# ------------------------------------------------------------------
#  本地磁盘缓存（带有效期）
#  ---------------------------------------------------------------
#  多源接口都是免费源，频繁重复拉取既慢又容易被限流；这里统一做
#  「带过期时间」的磁盘缓存，调用方按数据种类选择 TTL：
#    · 全市场快照行情   TTL 2 分钟（交易时段）/ 15 分钟（非交易时段）
#    · 日 K 线          TTL 25 分钟（交易时段）/ 12 小时（非交易时段）
#    · 指数             TTL 5 秒
#  缓存损坏/过期自动忽略，不会因为缓存问题阻塞主流程。
# ------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(PROJECT_DIR, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

_QUOTE_GATE = threading.Semaphore(6)
_CACHE_LOCK = threading.Lock()


def _cache_file(key: str) -> str:
    safe = key.replace("/", "_").replace("\\", "_")
    return os.path.join(CACHE_DIR, f"{safe}.json")


def cache_load(key: str, ttl: float):
    """读取缓存；不存在、损坏或超过 ttl 秒返回 None。"""
    path = _cache_file(key)
    try:
        if not os.path.exists(path):
            return None
        if ttl > 0 and time.time() - os.path.getmtime(path) > ttl:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_save(key: str, data) -> None:
    """原子写入缓存（先写临时文件再替换），失败不影响主流程。"""
    path = _cache_file(key)
    try:
        with _CACHE_LOCK:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
    except Exception:
        pass


def is_trading_time() -> bool:
    """A 股交易时段（含集合竞价与尾盘缓冲）。"""
    now = _dt_base.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H%M")
    return "0910" <= hm <= "1135" or "1255" <= hm <= "1505"


def _f(v, scale: float = 1.0) -> float:
    """宽松数值转换：'-' / None / 空串都返回 0。"""
    try:
        if v in (None, "", "-", "--"):
            return 0.0
        return float(v) * scale
    except Exception:
        return 0.0


# ------------------------------------------------------------------
#  凭证配置：通过环境变量注入，避免硬编码敏感信息
# ------------------------------------------------------------------
# 湘财证券 → Tushare token (在 https://tushare.pro 或湘财 Tushare 平台获取)
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
# 中国银河 → 格物 SDK 配置 (账号/密码/服务器，由银河证券提供)
GALAXY_USER = os.environ.get("GALAXY_USER", "")
GALAXY_PASS = os.environ.get("GALAXY_PASS", "")

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


# ============================================================
#  数据清洗与标准化
# ============================================================
def clean_and_normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    统一清洗流程：
      1. 列名标准化（中文→英文）
      2. 日期解析 + 去重 + 排序
      3. OHLCV 数值转换，异常值处理
      4. 缺失值填充（前向填充 + 线性插值）
      5. 成交量/额为 0 的异常 bar 标记或剔除
    """
    if df is None or df.empty:
        raise ValueError("输入数据为空")

    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
        "trade_date": "date",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"缺少必要字段: {missing}")

    # 日期
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df.set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]

    # 数值转换
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # 异常值：OHLC 必须 > 0
    for c in ["open", "high", "low", "close"]:
        df = df[df[c] > 0]

    # high >= low 校验，异常行丢弃
    df = df[df["high"] >= df["low"]]

    # 成交量缺失/为 0 用前值填充，避免除零
    if "volume" in df.columns:
        df["volume"] = df["volume"].replace(0, np.nan).ffill().fillna(0)

    # 价格缺失：前向填充 + 最后线性插值
    price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
    df[price_cols] = df[price_cols].ffill()
    df[price_cols] = df[price_cols].interpolate(method="linear", limit_direction="both")

    # 成交额：若缺失则用 close*volume 估算
    if "amount" not in df.columns or df["amount"].isna().all():
        df["amount"] = df["close"] * df["volume"]
    else:
        df["amount"] = df["amount"].ffill().fillna(df["close"] * df["volume"])

    return df[["open", "high", "low", "close", "volume", "amount"]]


# ============================================================
#  免费数据源
# ============================================================
def fetch_akshare_daily(symbol: str, start: str, end: str) -> pd.DataFrame:
    """akshare 日线（东方财富源）。"""
    import akshare as ak
    raw = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                             start_date=start, end_date=end, adjust="qfq")
    return clean_and_normalize(raw)


def fetch_eastmoney_daily(symbol: str, start: str, end: str) -> pd.DataFrame:
    """东方财富 HTTP API 直连日线。"""
    secid = f"1.{symbol}" if symbol.startswith(("6", "9")) else f"0.{symbol}"
    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101",
        "fqt": "1", "beg": start, "end": end,
    }
    r = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                     params=params, headers=_UA, timeout=(5, 12))
    r.raise_for_status()
    klines = (r.json().get("data") or {}).get("klines") or []
    if not klines:
        raise ValueError("东方财富未返回K线")
    rows = [line.split(",") for line in klines]
    cols = ["date", "open", "close", "high", "low", "volume", "amount"]
    df = pd.DataFrame([r[:7] for r in rows], columns=cols)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100  # 手→股
    return clean_and_normalize(df)


def fetch_tencent_daily(symbol: str, start: str, end: str) -> pd.DataFrame:
    """腾讯行情日线（兜底）。"""
    code = ("sh" if symbol.startswith("6") else "sz") + symbol
    fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,{fmt(start)},{fmt(end)},640,qfq")
    r = requests.get(url, headers=_UA, timeout=(4, 10))
    r.raise_for_status()
    data = r.json().get("data", {}).get(code, {})
    kline = data.get("qfqday") or data.get("day") or []
    if not kline:
        raise ValueError("腾讯未返回K线")
    df = pd.DataFrame(kline).iloc[:, :6]
    df.columns = ["date", "open", "close", "high", "low", "volume"]
    return clean_and_normalize(df)


def fetch_sina_daily(symbol: str, start: str, end: str) -> pd.DataFrame:
    """
    新浪日线（免费、稳定，沪/深/北交所均支持）。
    接口按 datalen 返回最近 N 根，再按起止日期裁剪；无成交额字段，
    由 clean_and_normalize 用 close*volume 估算。
    """
    import datetime as _dt
    prefix = ("sh" if symbol.startswith(("6", "9"))
              else ("bj" if symbol.startswith(("8", "4")) else "sz"))
    d0 = _dt.datetime.strptime(start, "%Y%m%d")
    d1 = _dt.datetime.strptime(end, "%Y%m%d")
    need = (d1 - d0).days + 30
    datalen = min(max(need, 100), 1000)

    r = requests.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        params={"symbol": prefix + symbol, "scale": "240", "ma": "no",
                "datalen": str(datalen)},
        headers={**_UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=(4, 10))
    data = r.json()
    if not data:
        raise ValueError("新浪未返回K线")
    df = pd.DataFrame(data).rename(columns={"day": "date"})
    lo = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    hi = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    df = df[(df["date"] >= lo) & (df["date"] <= hi)]
    if df.empty:
        raise ValueError("新浪K线在指定区间无数据")
    return clean_and_normalize(df)


def fetch_baostock_minute(symbol: str, start: str, end: str, freq: str = "5") -> pd.DataFrame:
    """baostock 分钟线（freq: 5/15/30/60）。"""
    import baostock as bs
    code = ("sh." if symbol.startswith("6") else "sz.") + symbol
    fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败：{lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,time,open,high,low,close,volume,amount",
            start_date=fmt(start), end_date=fmt(end), frequency=freq, adjustflag="2")
        if rs is None or rs.error_code != "0":
            raise RuntimeError(f"baostock 查询失败：{getattr(rs, 'error_msg', '')}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    if not rows:
        raise ValueError("baostock 未返回分钟线")
    df = pd.DataFrame(rows, columns=["date", "time", "open", "high", "low", "close", "volume", "amount"])
    df["time"] = df["time"].str.slice(8, 12)
    df = df[df["time"] >= "0930"].reset_index(drop=True)
    # 分钟线专用清洗：保留 date/time 列，数值转换、异常值处理
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["high"] >= df["low"]) & (df["close"] > 0)]
    df["volume"] = df["volume"].replace(0, np.nan).ffill().fillna(0)
    if "amount" in df.columns:
        df["amount"] = df["amount"].ffill().fillna(df["close"] * df["volume"])
    return df[["date", "time", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)


# ============================================================
#  湘财证券数据源（Tushare 合作平台）
# ============================================================
def fetch_xiangcai_daily(symbol: str, start: str, end: str,
                         token: Optional[str] = None) -> pd.DataFrame:
    """
    湘财证券 Tushare 数据平台日线。
    湘财与 Tushare 合作，数据接口与 Tushare Pro 一致。
    token 优先用参数，否则读环境变量 TUSHARE_TOKEN。
    """
    tok = token or TUSHARE_TOKEN
    if not tok:
        raise RuntimeError("湘财数据源需要 Tushare token，"
                           "请设置环境变量 TUSHARE_TOKEN 或传入 token 参数")
    try:
        import tushare as ts
    except ImportError:
        raise RuntimeError("使用湘财数据源需安装 tushare: pip install tushare")

    ts.set_token(tok)
    pro = ts.pro_api()
    # Tushare 代码格式：600000.SH
    ts_code = f"{symbol}.SH" if symbol.startswith(("6", "9")) else f"{symbol}.SZ"
    df = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
    if df is None or df.empty:
        raise ValueError("湘财/Tushare 未返回日线数据")
    # Tushare 字段：trade_date, open, high, low, close, vol, amount
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    # Tushare 的 amount 单位是千元，转为元
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce") * 1000
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100  # 手→股
    return clean_and_normalize(df)


# ============================================================
#  中国银河证券数据源（格物金融数据 SDK）
# ============================================================
def fetch_galaxy_daily(symbol: str, start: str, end: str,
                       user: Optional[str] = None,
                       password: Optional[str] = None) -> pd.DataFrame:
    """
    中国银河证券「格物」金融数据平台日线。
    需银河证券提供的 mdga SDK 及账号凭证。
    user/password 优先用参数，否则读环境变量 GALAXY_USER / GALAXY_PASS。
    """
    u = user or GALAXY_USER
    p = password or GALAXY_PASS
    if not u or not p:
        raise RuntimeError("银河数据源需要账号凭证，"
                           "请设置环境变量 GALAXY_USER / GALAXY_PASS 或传入参数")
    try:
        from mdga import MdApi  # 银河格物 SDK
    except ImportError:
        raise RuntimeError(
            "使用银河数据源需安装格物 SDK (mdga)。\n"
            "请从中国银河证券官网下载：\n"
            "  https://www.chinastock.com.cn → 量化交易 → 格物金融服务平台\n"
            "安装后将 mdga 包放入项目目录或 site-packages。")

    # 银河代码格式：600000 或 600000.SH
    code = f"{symbol}.SH" if symbol.startswith(("6", "9")) else f"{symbol}.SZ"
    api = MdApi()
    api.login(u, p)
    try:
        df = api.get_history_kline(
            code, start_date=start, end_date=end, period="1d", adjust="qfq")
    finally:
        api.logout()
    if df is None or df.empty:
        raise ValueError("银河格物未返回日线数据")
    return clean_and_normalize(df)


# ============================================================
#  统一调度：多源自动回退
# ============================================================
# ------------------------------------------------------------------
#  日线缓存与多通道回退
# ------------------------------------------------------------------
#  数据源优先级（日线）：
#    新浪（最快且沪深北通用） → 腾讯 → 东方财富 → akshare
#    → 湘财/Tushare（需 token） → 银河格物（需凭证）
#  扫描选股走 fetch_daily_fast（前三条，不含 akshare，避免首包 import 5 秒），
#  回测/自选走 fetch_daily 的完整链路。
# ------------------------------------------------------------------
_DAILY_SOURCES = {
    "sina": fetch_sina_daily,
    "akshare": fetch_akshare_daily,
    "eastmoney": fetch_eastmoney_daily,
    "tencent": fetch_tencent_daily,
    "xiangcai": fetch_xiangcai_daily,
    "galaxy": fetch_galaxy_daily,
}


def _kline_ttl() -> float:
    """日线缓存有效期：盘中 25 分钟（当日 K 线仍在变），盘后 12 小时。"""
    return 25 * 60 if is_trading_time() else 12 * 3600


def _df_to_records(df: pd.DataFrame) -> list:
    out = []
    for idx, row in df.iterrows():
        out.append({
            "date": idx.strftime("%Y-%m-%d"),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row["volume"]), "amount": float(row.get("amount", 0)),
        })
    return out


def _records_to_df(records: list) -> pd.DataFrame:
    return clean_and_normalize(pd.DataFrame(records))


def _load_kline_cache(symbol: str, start: str, end: str):
    key = f"kline_{symbol}_{start}_{end}"
    data = cache_load(key, _kline_ttl())
    if not data:
        return None, key
    try:
        return _records_to_df(data), key
    except Exception:
        return None, key


def _save_kline_cache(key: str, df: pd.DataFrame) -> None:
    try:
        cache_save(key, _df_to_records(df))
    except Exception:
        pass


def _try_chain(symbol: str, start: str, end: str, chain: list,
               quiet: bool = True) -> pd.DataFrame:
    errors = {}
    for name, loader in chain:
        try:
            df = loader(symbol, start, end)
            if df is None or df.empty:
                raise ValueError("返回空数据")
            return df
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(
        f"股票 {symbol}（{start}~{end}）数据源均失败："
        + "；".join(f"{k}: {v}" for k, v in errors.items()))


def fetch_daily(symbol: str, start: str, end: str,
                prefer: str = "auto", use_cache: bool = True) -> pd.DataFrame:
    """
    获取日线数据（带磁盘缓存），自动多源回退。
    prefer:
      - "auto"      : 新浪 → 腾讯 → 东财 → akshare → 湘财 → 银河
      - "fast"      : 新浪 → 腾讯 → 东财（不含 akshare，秒级返回）
      - 其他同数据源名：sina / akshare / eastmoney / tencent / xiangcai / galaxy
    """
    if prefer != "auto" and prefer != "fast":
        if prefer not in _DAILY_SOURCES:
            raise ValueError(f"未知数据源: {prefer}")
        cached, key = _load_kline_cache(symbol, start, end) if use_cache else (None, "")
        if cached is not None:
            return cached
        df = _DAILY_SOURCES[prefer](symbol, start, end)
        if use_cache and key:
            _save_kline_cache(key, df)
        return df

    cached, key = _load_kline_cache(symbol, start, end) if use_cache else (None, "")
    if cached is not None:
        return cached

    if prefer == "fast":
        chain = [("新浪", fetch_sina_daily),
                 ("腾讯", fetch_tencent_daily),
                 ("东方财富", fetch_eastmoney_daily)]
    else:
        chain = [("新浪", fetch_sina_daily),
                 ("腾讯", fetch_tencent_daily),
                 ("东方财富", fetch_eastmoney_daily),
                 ("akshare", fetch_akshare_daily)]
        if TUSHARE_TOKEN:
            chain.append(("湘财(Tushare)", fetch_xiangcai_daily))
        if GALAXY_USER and GALAXY_PASS:
            chain.append(("银河(格物)", fetch_galaxy_daily))

    df = _try_chain(symbol, start, end, chain)
    if use_cache and key:
        _save_kline_cache(key, df)
    return df


def fetch_daily_fast(symbol: str, start: str, end: str) -> pd.DataFrame:
    """扫描专用：仅走直连 HTTP 源，避免 akshare 首次 import 的 5 秒开销。"""
    return fetch_daily(symbol, start, end, prefer="fast", use_cache=True)


# ============================================================
#  全市场实时行情（东方财富 → 腾讯 → 本地缓存）
# ============================================================
_EM_FS = ("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,"
          "m:0+t:81+s:2048")     # 深主板/深中小/沪主板/科创/北交所
_EM_FIELDS = ("f2,f3,f5,f6,f8,f9,f12,f13,f14,f20,f21,f23,f24,f25,f100")


def _fetch_em_quote_page(page: int) -> list:
    params = {"pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
              "fid": "f12", "fs": _EM_FS, "fields": _EM_FIELDS}
    with _QUOTE_GATE:
        r = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
                         params=params, headers=_UA, timeout=(5, 10))
    data = (r.json() or {}).get("data") or {}
    return data.get("diff") or [], int(data.get("total") or 0)


def _quotes_from_eastmoney() -> dict:
    """东财全市场清单（1 次可取 100 只，分页并发；含换手/市值/行业）。"""
    first, total = _fetch_em_quote_page(1)
    pages = max(1, (total + 99) // 100)
    rows = list(first)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_fetch_em_quote_page, p)
                    for p in range(2, pages + 1)]
            for fut in as_completed(futs):
                try:
                    rows.extend(fut.result()[0])
                except Exception:
                    continue
    quotes = {}
    for d in rows:
        code = str(d.get("f12", ""))
        if not (len(code) == 6 and code.isdigit()):
            continue
        name = str(d.get("f14", "") or "")
        if not name:
            continue
        price = _f(d.get("f2"))
        if price <= 0:            # 停牌
            continue
        total_mv = _f(d.get("f20", 0), 1e-8)      # 元 → 亿元
        circ_mv = _f(d.get("f21", 0), 1e-8)
        pe = _f(d.get("f9"))
        pb = _f(d.get("f23"))
        quotes[code] = {
            "code": code, "name": name, "price": price,
            "change_pct": round(_f(d.get("f3")), 2),
            "amount": _f(d.get("f6")),             # 成交额（元）
            "volume": _f(d.get("f5")),             # 成交量（手）
            "turnover": round(_f(d.get("f8")), 2),  # 换手率 %
            "total_mv": round(total_mv, 2),         # 总市值（亿元）
            "circ_mv": round(circ_mv, 2),           # 流通市值（亿元）
            "pe": round(pe, 2) if pe > 0 else None,   # 市盈率（动态）
            "pb": round(pb, 2) if pb > 0 else None,   # 市净率
            "chg60": round(_f(d.get("f24")), 2),     # 60 日涨跌幅 %
            "chg_ytd": round(_f(d.get("f25")), 2),   # 年初至今涨跌幅 %
            "industry": str(d.get("f100", "") or ""),
            "is_st": ("ST" in name) or ("退" in name),
            "is_new": name.upper().startswith(("N", "C")),
        }
    if not quotes:
        raise ValueError("东方财富清单为空")
    return quotes


def _prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return "sz" + code


def _quotes_from_tencent(codes: list, workers: int = 6) -> dict:
    """腾讯批量行情（兜底，一次 50 只；不含行业字段）。"""
    quotes = {}

    def fetch_batch(batch):
        url = "https://qt.gtimg.cn/q=" + ",".join(_prefix(c) for c in batch)
        try:
            with _QUOTE_GATE:
                r = requests.get(url, headers=_UA, timeout=(4, 8))
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if '"' not in line:
                    continue
                f = line.split('"')[1].split("~")
                if len(f) <= 45 or not f[3]:
                    continue
                code = f[2]
                quotes[code] = {
                    "code": code, "name": f[1], "price": _f(f[3]),
                    "prev_close": _f(f[4]), "open": _f(f[5]),
                    "high": _f(f[33]), "low": _f(f[34]),
                    "change_pct": round(_f(f[32]), 2),
                    "amount": _f(f[37], 1e4),       # 万元 → 元
                    "volume": _f(f[6]),
                    "turnover": round(_f(f[38]), 2),
                    "total_mv": _f(f[45]),          # 亿元
                    "circ_mv": _f(f[44]),
                    "industry": "",
                    "is_st": ("ST" in f[1]) or ("退" in f[1]),
                    "is_new": f[1].upper().startswith(("N", "C")),
                }
        except Exception:
            pass

    batches = [codes[i:i + 50] for i in range(0, len(codes), 50)]
    if batches:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(fetch_batch, batches))
    return quotes


def fetch_quotes_batch(codes: list) -> dict:
    """按代码批量取实时报价（腾讯，50 只/请求），供界面秒级刷新。

    只返回请求成功且有价格的股票，字段与 fetch_all_quotes 一致。
    """
    codes = [str(c) for c in codes if len(str(c)) == 6 and str(c).isdigit()]
    if not codes:
        return {}
    try:
        return _quotes_from_tencent(codes, workers=3)
    except Exception:
        return {}


def _fallback_code_list() -> list:
    """兜底代码表：缓存 → akshare（仅在两个行情源都失败时使用）。"""
    cached = cache_load("code_list", 7 * 24 * 3600)
    if isinstance(cached, list) and cached:
        return cached
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        codes = [str(c) for c in df["code"].tolist()]
        cache_save("code_list", codes)
        return codes
    except Exception:
        return []


def fetch_all_quotes(force: bool = False, save_cache: bool = True) -> dict:
    """
    全市场实时行情（统一字段）：
      code,name,price,change_pct,amount(元),volume,turnover(%),
      total_mv(亿),circ_mv(亿),industry,is_st,is_new
    源优先级：东方财富 → 腾讯 → 上次磁盘缓存。
    缓存 TTL：盘中 2 分钟、非盘中 15 分钟；force=True 时强制刷新。
    """
    ttl = 0 if force else (120 if is_trading_time() else 900)
    key = f"quotes_all_{_dt_base.date.today():%Y%m%d}"
    if not force:
        cached = cache_load(key, ttl)
        if cached:
            return cached

    try:
        quotes = _quotes_from_eastmoney()
    except Exception:
        quotes = {}
        codes = _fallback_code_list()
        if not codes:
            old = cache_load(key, 0) or cache_load(
                f"quotes_all_{_dt_base.date.today():%Y%m%d}", 0)
            if old:
                return old
            raise RuntimeError("全市场行情获取失败（东财/腾讯/缓存均不可用）")
        try:
            quotes = _quotes_from_tencent(codes)
        except Exception:
            quotes = {}

    if quotes:
        if save_cache:
            cache_save(key, quotes)
        return quotes

    old = cache_load(key, 0)
    if old:
        return old
    raise RuntimeError("全市场行情获取失败")


def market_of_code(code: str) -> str:
    """代码 → 交易所（与页面展示口径一致）。"""
    if code.startswith(("60", "68", "9")):
        return "上交所"
    if code.startswith(("00", "30", "20")):
        return "深交所"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "未知"


def fetch_minute(symbol: str, start: str, end: str, freq: str = "5") -> pd.DataFrame:
    """获取分钟线（当前 baostock 免费源，后续可扩展湘财/银河分钟线）。"""
    return fetch_baostock_minute(symbol, start, end, freq)


# ============================================================
#  实时快照（新浪，东财兜底）
# ============================================================
def fetch_realtime_snapshot(symbol: str) -> dict:
    """实时行情快照。"""
    secid = f"sh{symbol}" if symbol.startswith(("6", "9")) else f"sz{symbol}"
    try:
        r = requests.get(f"https://hq.sinajs.cn/list={secid}",
                         headers={**_UA, "Referer": "https://finance.sina.com.cn/"},
                         timeout=(5, 10))
        r.encoding = "gbk"
        body = r.text.split('"')[1] if '"' in r.text else ""
        f = body.split(",")
        if len(f) < 32:
            raise ValueError("新浪快照字段不足")
        prev = float(f[2])
        price = float(f[3])
        return {
            "name": f[0], "open": float(f[1]), "prev_close": prev,
            "price": price, "high": float(f[4]), "low": float(f[5]),
            "volume": float(f[8]), "amount": float(f[9]),
            "change_pct": round((price / prev - 1) * 100, 2) if prev else 0,
            # 新浪最后两个字段为行情日期与时间，用于判断是否为当日盘中数据
            "quote_date": f[30] if len(f) > 30 else "",
            "quote_time": f[31] if len(f) > 31 else "",
        }
    except Exception:
        # 东财兜底
        eid = f"1.{symbol}" if symbol.startswith(("6", "9")) else f"0.{symbol}"
        r = requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                         params={"secid": eid,
                                 "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60"},
                         headers=_UA, timeout=10)
        d = (r.json() or {}).get("data") or {}
        return {
            "name": d.get("f58"), "price": d.get("f43", 0) / 100,
            "prev_close": d.get("f60", 0) / 100, "high": d.get("f44", 0) / 100,
            "low": d.get("f45", 0) / 100, "open": d.get("f46", 0) / 100,
            "volume": d.get("f47", 0), "amount": d.get("f48", 0),
        }


if __name__ == "__main__":
    # 快速自测：九牧王 601566 日线
    try:
        df = fetch_daily("601566", "20240401", "20261016")
        print(df.tail())
        print(f"共 {len(df)} 条，{df.index[0].date()} ~ {df.index[-1].date()}")
    except Exception as e:
        print(f"取数失败: {e}")
