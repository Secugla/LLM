# -*- coding: utf-8 -*-
"""
机构量化策略学习库（第六功能模块的数据层）
============================================
说明：量化机构（幻方、九坤、明汯、灵均等）的真实因子公式、权重与持仓
均属商业机密，从未完整公开。本模块只做三件事：
  1) 整理各机构「官方公开信息」中可核实的方法论描述（公开事实）；
  2) 收录媒体/研究者的分析推断，并明确标注「未证实」；
  3) 把上述思想映射到本系统已有的 8 个可计算因子，给出可运行的近似权重，
     供用户在「策略学习」页一键套用到真实选股。
因此本模块输出的是「公开信息学习 + 近似实现」，不代表、也无法复现
任何机构的真实策略。

权重约定：8 个因子的权重之和为 100，套用时按权重重新分配各因子满分，
再按比例缩放原始因子得分（不改变因子内部的相对强弱）。
"""

# 本系统因子键（与 recommender.FACTOR_META 对应）
FACTOR_KEYS = ["trend", "volume", "liquidity", "rotation",
               "leader", "theme", "capital", "risk"]
FACTOR_NAME = {
    "trend": "趋势动量", "volume": "量价配合", "liquidity": "流动性",
    "rotation": "板块轮动", "leader": "龙头强度", "theme": "题材热度",
    "capital": "资金强度", "risk": "风险质量",
}

# 公开来源（统一在此登记，页面里按策略展示）
SRC = {
    "hf_official": ("幻方量化官网 · 投资理念/基金页",
                    "https://www.high-flyer.cn/fund/"),
    "hf_media": ("雪球 · 幻方“三层 AI 金字塔”市场解读（未证实）",
                 "https://xueqiu.com/1680367052/398490543"),
    "hf_media2": ("东方财富 · 幻方策略拆解类文章（未证实）",
                  "https://emcreative.eastmoney.com/app_fortune/article/"
                  "index.html?artCode=20260228104019695954610"),
    "ubq_official": ("九坤投资官网 · 关于我们/业务",
                     "https://www.ubiquant.com/website/about"),
    "mh_media": ("雪球 · 明汯多周期多策略体系解读（媒体分析）",
                 "https://xueqiu.com/7656313923/205483612"),
    "lj_media": ("中国证券报 · 灵均投资基本面因子体系报道",
                 "https://www.cs.com.cn/tzjj/jjdt/202508/t20250811_6506211.html"),
    "wq_paper": ("WorldQuant · 101 Formulaic Alphas（论文）",
                 "https://arxiv.org/pdf/1601.00991v2"),
    "wq_review": ("Alpha101 公式中文综述（第三方整理）",
                  "https://coriva.eu.org/en/alpha101-overview/"),
}

STRATEGIES = [
    {
        "id": "high_flyer",
        "org": "幻方量化 · High-Flyer",
        "name": "幻方：AI 全流程 + 多源数据",
        "tag": "公开事实 + 媒体推断（未证实）",
        "one_liner": "用机器学习/深度学习从海量价量、基本面与另类数据中挖掘因子，"
                     "再由模型组合成信号，并叠加严格风险控制。",
        "public_facts": [
            "官网披露：2008 年起探索 AI/机器学习，2016-10-21 上线首个由深度学习"
            "生成的实盘仓位，2017 年实现深度学习全流程交易。",
            "自建“萤火”AI 训练平台，累计数据达 10PB 级别、接入数十个数据源。",
            "定价同时使用基本面、另类数据与行情数据；采用逆向交易以平抑非理性"
            "波动、为市场提供流动性。",
            "未公开任何因子公式、因子权重、持仓明细或模型结构。",
        ],
        "inferred": [
            "媒体拆解称其存在“三层 AI 金字塔”：遗传算法因子工厂、强化学习策略"
            "生成、CVaR 风险层（未证实）。",
            "市场估算其价量类因子权重约四成、基本面约三成、另类/新闻约两成"
            "（数字来自媒体推测，无官方口径，不可当作事实）。",
            "“萤火”平台与逆向交易属于官方口径，但具体如何落到单票打分不可知。",
        ],
        "mapping": {
            "trend": "20 日/5 日动量与均线结构，近似价量类 alpha 的趋势部分",
            "volume": "量比、换手与量价同步，近似价量类 alpha 的量能部分",
            "liquidity": "成交额与市值区间，对应机构的流动性约束",
            "rotation": "板块涨幅与涨停动量，近似行业/主题轮动信号",
            "leader": "连板高度与封单强度，近似强弱分层与拥挤度",
            "theme": "F10 题材与新闻匹配，近似另类/文本数据",
            "capital": "成交额与突破位置，近似资金强度代理",
            "risk": "ST/新股/高位/破位/波动剔除，近似 CVaR 风险层",
        },
        "data_needs": [
            "全市场行情快照（成交额、换手、市值、行业）",
            "候选股 520 交易日日线（用于 20/5 日动量、MA、周线）",
            "涨停池与板块轮动数据",
            "东财 F10 核心题材与当日新闻（7 天缓存）",
        ],
        "risks": [
            "价量因子在极端行情下容易拥挤、失效；",
            "公开资料只描述方向，无法校准真实参数；",
            "缺少逐笔/席位数据，资金因子为近似值。",
        ],
        "sources": ["hf_official", "hf_media", "hf_media2"],
        "weights": {"trend": 28, "volume": 20, "liquidity": 8, "rotation": 8,
                    "leader": 10, "theme": 6, "capital": 10, "risk": 10},
        "params": {"top_n": 20, "max_candidates": 150,
                   "industry_cap": 5, "min_score": 35},
    },
    {
        "id": "ubiquant",
        "org": "九坤投资 · Ubiquant",
        "name": "九坤：多因子 + 指数增强",
        "tag": "官方方法论 + 本系统近似",
        "one_liner": "以多因子模型做全市场选股，持仓数百只高度分散，"
                     "再用股指期货对冲，追求相对指数的稳定超额。",
        "public_facts": [
            "官网业务包含指数增强、量化对冲、CTA、多空与选股。",
            "指数增强采用多因子模型，持仓数百只股票做分散化。",
            "对冲端使用股指期货等工具控制市场暴露。",
            "官网未公开因子明细与权重。",
        ],
        "inferred": [
            "公开访谈与行业报道普遍提及其偏好中低频、重视因子稳健性与"
            "风险中性（媒体口径，未证实具体参数）。",
            "指数增强的行业/市值暴露约束，可近似为本系统的行业分散与流动性过滤。",
        ],
        "mapping": {
            "trend": "动量/趋势因子，控制与基准的偏离",
            "volume": "量价因子，权重适中避免换手过高",
            "liquidity": "流动性是硬约束，权重较高（大资金容量优先）",
            "rotation": "行业轮动暴露，做适度中性化",
            "leader": "个股强弱分层，避免极端集中",
            "theme": "主题暴露作为卫星因子",
            "capital": "资金强度辅助确认",
            "risk": "风险因子权重较高，强调回撤控制与分散",
        },
        "data_needs": [
            "全市场行情（成交额/流通市值/行业）",
            "日线与周线（趋势、量价、波动）",
            "板块轮动与涨停池",
        ],
        "risks": [
            "缺少真实指数成分与行业中性约束，无法复现指数增强；",
            "本系统未接入期货，无法做对冲；",
            "分散化靠行业上限近似，不是真正的风险模型。",
        ],
        "sources": ["ubq_official"],
        "weights": {"trend": 14, "volume": 8, "liquidity": 18, "rotation": 14,
                    "leader": 10, "theme": 8, "capital": 10, "risk": 18},
        "params": {"top_n": 20, "max_candidates": 200,
                   "industry_cap": 5, "min_score": 40},
    },
    {
        "id": "minghong",
        "org": "明汯投资 · Minghong",
        "name": "明汯：多周期 / 多频率 / 多策略",
        "tag": "媒体分析 + 本系统近似",
        "one_liner": "把低频选股与日内 T0 结合，多周期、多频率、多策略并行，"
                     "用量价与流动性捕捉短周期机会。",
        "public_facts": [
            "媒体对其体系的描述为多周期、多频率、多策略并行。",
            "公开报道提到低频选股叠加日内 T0 交易。",
        ],
        "inferred": [
            "短周期价量因子权重较高、换手较快（媒体口径，未证实）。",
            "T0 依赖盘口与高频数据，本系统只有日线/快照，无法实现。",
        ],
        "mapping": {
            "trend": "短周期动量与均线结构",
            "volume": "量价配合是核心，权重最高",
            "liquidity": "流动性/成交额门槛较高，保证可交易",
            "rotation": "板块轮动作为中周期方向",
            "leader": "强势股与连板梯队",
            "theme": "题材热度作为短周期催化",
            "capital": "资金强度确认",
            "risk": "风险权重最低（偏进攻），但保留硬剔除",
        },
        "data_needs": [
            "全市场快照（重点成交额与换手）",
            "日线（量比、突破、动量）",
            "涨停池与板块轮动",
        ],
        "risks": [
            "无法实现日内 T0，持有周期被拉长；",
            "高换手策略对交易成本敏感；",
            "偏进攻权重会放大回撤。",
        ],
        "sources": ["mh_media"],
        "weights": {"trend": 15, "volume": 20, "liquidity": 18, "rotation": 10,
                    "leader": 10, "theme": 8, "capital": 12, "risk": 7},
        "params": {"top_n": 15, "max_candidates": 150,
                   "industry_cap": 3, "min_score": 40},
    },
    {
        "id": "lingjun",
        "org": "灵均投资 · Lingjun",
        "name": "灵均：基本面因子 + 广覆盖子策略",
        "tag": "媒体分析 + 本系统近似",
        "one_liner": "自 2015 年起把基本面因子纳入体系，用覆盖广泛的子策略"
                     "分散单一因子失效风险。",
        "public_facts": [
            "公开报道：自 2015 年起布局基本面因子。",
            "子策略覆盖较广，强调多策略组合而非单一信号。",
        ],
        "inferred": [
            "基本面与低换手因子占比更高，价量因子做补充（媒体口径，未证实）。",
            "偏好稳健、回撤可控的权重结构。",
        ],
        "mapping": {
            "trend": "中期趋势与均线多头",
            "volume": "量价确认，权重中等",
            "liquidity": "流动性约束",
            "rotation": "行业轮动权重较高，近似基本面行业配置",
            "leader": "个股质地与强势程度",
            "theme": "主题/新闻作为卫星因子",
            "capital": "资金强度辅助",
            "risk": "风险权重高，强调稳健",
        },
        "data_needs": [
            "全市场行情与行业分类",
            "日线/周线趋势",
            "板块轮动、题材与新闻",
        ],
        "risks": [
            "本系统无财务数据，基本面只能用行业轮动与风险质量近似；",
            "无法复现其多子策略组合方式。",
        ],
        "sources": ["lj_media"],
        "weights": {"trend": 18, "volume": 8, "liquidity": 12, "rotation": 14,
                    "leader": 10, "theme": 12, "capital": 8, "risk": 18},
        "params": {"top_n": 20, "max_candidates": 200,
                   "industry_cap": 4, "min_score": 40},
    },
    {
        "id": "worldquant101",
        "org": "WorldQuant · Alpha101",
        "name": "WorldQuant：101 个公式化 Alpha",
        "tag": "公开论文 + 可复现思想",
        "one_liner": "公开 101 个价量公式化 alpha，论文称其中约 80 个进入生产，"
                     "持仓周期 0.6~6.4 天，alpha 间平均相关性约 15.9%。",
        "public_facts": [
            "论文《101 Formulaic Alphas》公开了 101 个 alpha 公式。",
            "输入变量为 open/high/low/close/volume/vwap/cap/adv 等公开行情字段。",
            "论文披露：约 80 个 alpha 进入生产，平均持仓 0.6~6.4 天，"
            "alpha 两两平均相关性约 15.9%。",
        ],
        "inferred": [
            "其核心是短周期价量价差、量能反转与排名类算子（rank/ts_rank）。",
            "免费数据缺少可靠 vwap，部分公式无法精确复现（公开事实约束）。",
        ],
        "mapping": {
            "trend": "短周期反转/动量类 alpha 的近似",
            "volume": "量能类 alpha 权重最高（对应 101 中的量价主体）",
            "liquidity": "adv/cap 类流动性输入",
            "rotation": "行业截面排名近似",
            "leader": "截面强势排名近似",
            "theme": "本系统以题材/新闻替代文本 alpha",
            "capital": "成交额与位置类 alpha",
            "risk": "波动与回撤约束",
        },
        "data_needs": [
            "日线 OHLCV（本系统用新浪/腾讯/东财多源）",
            "全市场快照（cap、adv、换手）",
            "注意：无可靠 vwap，vwap 类公式以均价近似",
        ],
        "risks": [
            "短周期 alpha 换手高，未计交易成本会高估收益；",
            "公式在 A 股需重新校准，直接照搬可能失效；",
            "本系统只实现思想映射，不是 101 个公式的逐条复刻。",
        ],
        "sources": ["wq_paper", "wq_review"],
        "weights": {"trend": 12, "volume": 30, "liquidity": 16, "rotation": 6,
                    "leader": 6, "theme": 6, "capital": 10, "risk": 14},
        "params": {"top_n": 20, "max_candidates": 200,
                   "industry_cap": 3, "min_score": 38},
    },
    {
        "id": "classic_multi_factor",
        "org": "通用 · 经典多因子框架",
        "name": "经典多因子：动量 + 价值 + 质量 + 低波",
        "tag": "教科书框架 + 本系统近似",
        "one_liner": "动量、价值、质量、低波动四类因子做截面打分，"
                     "配合行业中性、流动性过滤与风险剔除。",
        "public_facts": [
            "多因子模型是量化选股的主流框架：因子打分 → 合成 → 组合优化。",
            "常见约束包括行业中性、市值中性、换手与流动性下限。",
        ],
        "inferred": [
            "A 股短周期下动量与量价更有效，价值/质量需要财务数据（本系统缺失）。",
            "低波与风险剔除可以显著降低回撤（行业共识，非某机构专有）。",
        ],
        "mapping": {
            "trend": "动量因子",
            "volume": "量价因子",
            "liquidity": "流动性/换手约束",
            "rotation": "行业轮动（近似行业中性后的超配）",
            "leader": "质量/强势代理",
            "theme": "主题暴露",
            "capital": "资金因子",
            "risk": "低波与风险剔除，权重最高",
        },
        "data_needs": [
            "全市场行情",
            "日线/周线",
            "板块与题材",
        ],
        "risks": [
            "无财务数据，价值/质量只能弱代理；",
            "未做严格的行业市值中性化；",
            "因子权重为经验值，需要长期回测校准。",
        ],
        "sources": [],
        "weights": {"trend": 15, "volume": 12, "liquidity": 12, "rotation": 10,
                    "leader": 8, "theme": 8, "capital": 10, "risk": 25},
        "params": {"top_n": 20, "max_candidates": 200,
                   "industry_cap": 5, "min_score": 40},
    },
]


def list_strategies() -> list:
    """返回全部策略（按列表顺序）。"""
    return STRATEGIES


def get_strategy(sid: str) -> dict:
    """按 id 取策略，找不到返回第一条。"""
    for s in STRATEGIES:
        if s["id"] == sid:
            return s
    return STRATEGIES[0]


def validate_weights(weights: dict) -> dict:
    """校验并归一化权重：缺省因子补 0，总和归一到 100。"""
    w = {k: max(0.0, float(weights.get(k, 0) or 0)) for k in FACTOR_KEYS}
    total = sum(w.values())
    if total <= 0:
        return {k: 100.0 / len(FACTOR_KEYS) for k in FACTOR_KEYS}
    return {k: round(v * 100.0 / total, 2) for k, v in w.items()}


def source_links(s: dict) -> list:
    """把 sources 键转成 (标题, 链接) 列表。"""
    return [SRC[k] for k in s.get("sources", []) if k in SRC]
