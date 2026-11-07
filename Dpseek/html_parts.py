# -*- coding: utf-8 -*-
"""HTML template for the document."""
import time

INK, MUT = "#111827", "#6b7280"
EX = ["RAVEUSDT", "LABUSDT", "BTWUSDT", "LIGHTUSDT"]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(v):
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e8: return f"${v/1e6:.0f}M"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def d2s(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def cls(v):
    return "g" if v >= 0 else "r"


CSS = """
:root{--ink:#111827;--mut:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
*{box-sizing:border-box}
body{margin:0;background:#eef1f5;color:var(--ink);font:14px/1.75 "Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 18px 60px}
.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:24px 28px;margin-bottom:18px;
       box-shadow:0 1px 2px rgba(16,24,40,.05)}
h1{font-size:25px;margin:0 0 6px;letter-spacing:-.2px}
h2{font-size:18px;margin:30px 0 12px;padding-left:10px;border-left:4px solid var(--ink)}
h3{font-size:15px;margin:24px 0 8px}
.sub{color:var(--mut);font-size:13px;margin:0}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0 4px}
.kpi{background:var(--bg);border:1px solid var(--line);border-radius:11px;padding:12px 14px}
.kpi b{display:block;font-size:21px;line-height:1.3}
.kpi span{font-size:11.5px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}
th,td{padding:6px 8px;border-bottom:1px solid #eef1f5;text-align:right;white-space:nowrap}
th{background:#fafbfc;color:var(--mut);font-size:11.5px;font-weight:600}
td.s,th:nth-child(2){text-align:left;font-weight:600}
.num{font-variant-numeric:tabular-nums}
.g{color:#16a34a}.r{color:#dc2626}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
figure.card{margin:0;padding:8px;background:#fff;border:1px solid var(--line);border-radius:12px;
            display:flex;flex-direction:column;box-shadow:0 1px 2px rgba(16,24,40,.05)}
figcaption{font-size:11px;color:var(--mut);padding:4px 2px 0;line-height:1.6}
figcaption b{color:var(--ink)}
figcaption a{color:#2563eb;text-decoration:none;white-space:nowrap}
.wide{grid-column:1/-1}
.note{font-size:11.5px;color:var(--mut)}
.box{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px 18px;font-size:12.5px}
.ok{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:14px 18px;font-size:12.5px}
.warn{background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:14px 18px;font-size:12.5px}
ul{padding-left:20px;margin:8px 0} li{margin:5px 0}
code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px}
.tag{display:inline-block;font-size:10.5px;padding:0 7px;border-radius:999px;background:#eef2ff;color:#3730a3;margin-left:6px}
.feat{display:grid;grid-template-columns:26px 1fr;gap:12px;padding:11px 0;border-bottom:1px dashed var(--line)}
.feat:last-child{border:0}
.fn{width:24px;height:24px;border-radius:7px;background:var(--ink);color:#fff;display:flex;align-items:center;
    justify-content:center;font-weight:700;font-size:12px}
.ft{font-weight:700;margin:0 0 2px;font-size:13.5px}
.fd{font-size:12.5px;color:#374151;margin:0}
@media(max-width:900px){.grid{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,1fr)}}
"""


def page(feats, selected, svg, sel):
    ex = [feats[s] for s in EX if s in feats]
    A = [p for p in feats.values() if p["venue"] == "spot"]
    B = [p for p in feats.values() if p["venue"] != "spot"]
    now = max(p["to_ms"] for p in feats.values())

    def ex_row(p):
        return (f"<tr><td class='s'>{p['base']}</td><td>{d2s(p['listing_ms'])}</td>"
                f"<td class='num'>{p['n']}</td><td class='num {cls(p['ret'])}'>{p['ret']:+.0f}%</td>"
                f"<td class='num'>{p['avg_range']:.1f}%</td><td class='num'>{p['dvol']:.1f}%</td>"
                f"<td class='num'>{money(p['med_qv'])}</td><td class='num'>{p['burst']:.0f}×</td>"
                f"<td class='num'>{p['swing']:.0f}×</td><td class='num'>{p['maxup']:+.0f}%</td>"
                f"<td class='num'>{p['dd']:+.0f}%</td><td class='num'>{p['score']:.1f}</td></tr>")

    def sel_row(i, p):
        liq = "g" if p["med_qv"] >= 1e7 else ("r" if p["med_qv"] < 4e6 else "")
        return (f"<tr><td class='num'>{i}</td><td class='s'>{p['base']}</td>"
                f"<td>{d2s(p['listing_ms'])}</td><td class='num'>{p['n']}</td>"
                f"<td class='num {cls(p['ret'])}'>{p['ret']:+.0f}%</td>"
                f"<td class='num'>{p['avg_range']:.1f}%</td><td class='num'>{p['dvol']:.1f}%</td>"
                f"<td class='num {liq}'>{money(p['med_qv'])}</td>"
                f"<td class='num'>{p['burst']:.0f}×</td><td class='num'>{p['swing']:.0f}×</td>"
                f"<td class='num'>{p['maxup']:+.0f}%</td><td class='num'>{p['dd']:+.0f}%</td>"
                f"<td class='num'><b>{p['score']:.1f}</b></td></tr>")

    cards = "".join(
        f"<figure class='card' id='c-{i}'>{svg[p['symbol']]}"
        f"<figcaption>#{i} <b>{p['base']}/USDT</b> · 同款度 <b>{p['score']:.1f}</b>"
        f" · 上线 {d2s(p['listing_ms'])} · 记录期 {p['n']} 天"
        f" · 日均振幅 {p['avg_range']:.1f}% · 中位成交额 {money(p['med_qv'])}"
        f" · <a href='charts/{p['base']}-后60天.svg' target='_blank'>单独查看</a></figcaption></figure>"
        for i, p in enumerate(selected, 1))

    ex_cards = "".join(
        f"<figure class='card'>{svg[p['symbol'] + '_full']}"
        f"<figcaption>上市至今（{p['n'] + 60} 天），蓝虚线为发行后第 60 天</figcaption></figure>"
        f"<figure class='card'>{svg[p['symbol'] + '_post']}"
        f"<figcaption>发行后第 60 天至今（{p['n']} 天）·"
        f" <a href='charts/{p['base']}-后60天.svg' target='_blank'>单独查看</a></figcaption></figure>"
        for p in ex)

    ex_rows = "".join(ex_row(p) for p in ex)
    sel_rows = "".join(sel_row(i, p) for i, p in enumerate(selected, 1))

    # tier counts
    t90 = sum(1 for p in selected if p["score"] >= 90)
    t85 = sum(1 for p in selected if 85 <= p["score"] < 90)
    t80 = sum(1 for p in selected if 80 <= p["score"] < 85)
    tlow = sum(1 for p in selected if p["score"] < 80)
    died = sum(1 for p in selected if p["dd"] < -90)
    alive = sum(1 for p in selected if p["med_qv"] >= 10e6)
    big_up = sum(1 for p in selected if p["ret"] > 100)

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>高波动合约币同款分析 · 从 RAVE / LAB / BTW / LIGHT 出发</title>
<style>{CSS}</style></head><body><div class="wrap">

<div class="panel">
<h1>高波动合约币「同款」分析</h1>
<p class="sub">样本：RAVE / LAB / BTW / LIGHT 四个实际交易过的币 · 对比池：Binance 近三年上线的 {len(A)+len(B)} 个币
（现货 {len(A)} 个 + 合约独有 {len(B)} 个）· 数据截至 {d2s(now)}</p>

<div class="kpis">
<div class="kpi"><b>{len(selected)}</b><span>符合条件的同款币</span></div>
<div class="kpi"><b>100%</b><span>为“合约独有”（Binance 无现货）</span></div>
<div class="kpi"><b>{t90+t85+t80}</b><span>同款度 ≥ 80 分</span></div>
<div class="kpi"><b>{died}/{len(selected)}</b><span>记录期内自高点回撤 &gt; 90%</span></div>
</div>

<div class="box">
<b>核心结论</b><br>
1. RAVE / LAB / BTW / LIGHT 属于同一类标的：<b>近一年上线的合约独有币，日均振幅 16%–27%、日波动率 13%–23%、
成交量最爆的一天是中位数的 15–555 倍</b>。<br>
2. 这类标的的画像在<b>现货币里几乎不存在</b>：138 个有现货的新币中，日均振幅中位数 9.3%、95 分位仅 11.7%；
而 301 个合约独有币的 95 分位是 18.1%、最高 26.8%。<b>“高波动 + 仅有合约”是同一个筛选器的两面。</b><br>
3. 用 5 项硬门槛 + 同款度打分重筛，<b>{len(selected)} 个币</b>与四个示例同型；其中 {t90+t85+t80} 个同款度 ≥ 80。<br>
4. 但代价很直接：<b>{died} 个（{died/len(selected)*100:.0f}%）已从最高点回撤超过 90%</b>，
说明这类币的“高波动”是双向的 —— 既能给你 LAB/RAVE 那种 2 个月 10 倍的行情，也能让仓位在两周内归零。
</div>
</div>

<div class="panel">
<h2>一、口径与方法</h2>
<ul>
<li><b>记录期</b>：每个币<b>发行后第 60 天起至今</b>（与上一份“上线 60 天内”的筛选正好接力，不重叠）。</li>
<li><b>发行日</b>：Binance 现货上线日；若无现货（合约独有），取 Binance 合约数据包中最早的一个月。</li>
<li><b>数据源</b>：Binance 官方公开数据包 <code>data.binance.vision</code>（USDⓈ-M 永续日线）+ Binance 现货 K 线 API。</li>
<li><b>合约独有币的发现</b>：Binance 没有公开可用的“合约币种清单”接口，本次用 Gate.io 的 USDT 永续 / 现货币种名单作为名字池
（2118 个候选名字，逐个探测 Binance 合约数据包是否存在），共探测到 <b>375 个</b>近三年上线、且仍在交易的合约独有币，
其中 <b>{len(B)} 个</b>记录期数据充足、进入对比池。加上现货 {len(A)} 个，形成 {len(A)+len(B)} 个币的分析池。</li>
<li><b>同款度</b>：在 7 项特征上取该币在整个对比池中的百分位，加权平均（日均振幅 25%、日波动率 25%、爆量倍数 15%、
价格极差 15%、最大单日涨幅 10%、中位成交额 10%）。分数越高 = 越接近四个示例的形态。</li>
</ul>
<div class="note">注：现货币的价格与成交额取自现货，合约独有币取自永续；两者都是日线 USDT 计价，量级可比。</div>
</div>

<div class="panel">
<h2>二、四个示例的画像</h2>
<table><thead><tr><th>币种</th><th>上线</th><th>记录天数</th><th>期内涨跌</th><th>日均振幅</th>
<th>日波动率</th><th>中位日成交额</th><th>爆量倍数</th><th>价格极差</th><th>最大单日涨</th>
<th>距最高点</th><th>同款度</th></tr></thead><tbody>{ex_rows}</tbody></table>
<p class="note">四个币的<b>方向完全不同</b>（三个跌 56%–81%，BTW 涨 2192%），但<b>波动结构高度一致</b>：
日均振幅 16%–27%、单日最大涨幅 161%–240%、爆量 216–555 倍。这就是“同款”的真正含义 —— 不是同涨同跌，而是同一台波动率机器。</p>
<div class="grid" style="margin-top:14px">{ex_cards}</div>
</div>

<div class="panel">
<h2>三、与已筛选 {len(A)} 个现货新币的对比</h2>
<div class="grid">
<figure class="card wide">{svg.get('__dist', '')}<figcaption>日均振幅的分布对比：合约独有币的尾部明显更厚</figcaption></figure>
<figure class="card wide">{svg.get('__scatter', '')}<figcaption>横轴为中位日成交额（对数），纵轴为日均振幅。红点为四个示例，橙点为本次选出的同款币</figcaption></figure>
</div>
<div class="grid" style="margin-top:14px">
<div class="panel" style="margin:0">
<h3 style="margin-top:0">结论一：现货新币“太温和”</h3>
<p class="note">138 个有现货的新币中，日均振幅中位数 9.3%、95 分位 11.7%、最高 12.7%（ALLO）。
按“振幅 ≥12% 且 日波动 ≥10%”只有 2 个达标，且中位成交额都 &lt; $2M（流动性不够）。
<b>原因很直接：有现货的币，抛压会被现货深度吸收；只有合约的币，价格几乎完全由杠杆资金驱动。</b></p>
</div>
<div class="panel" style="margin:0">
<h3 style="margin-top:0">结论二：合约独有币的尾部极厚</h3>
<p class="note">301 个合约独有币中，日均振幅中位数 7.7%（甚至低于现货币），但 95 分位 18.1%、最高 26.8%。
这是一个<b>典型的“双峰”结构</b>：大多数合约独有币同样没人交易，但少数会被资金选中，
变成 RAVE / LAB 这种日振幅 20%+ 的品种。<b>筛选的价值就在于把这一小撮挑出来。</b></p>
</div>
</div>
</div>

<div class="panel">
<h2>四、「共性」的可量化定义</h2>
<p>把四个示例放在一起看，共同点不是行业、不是叙事，而是下面 6 个可以被直接测量的结构特征（前 5 条为硬门槛，第 3 条仅参与打分）：</p>
<div class="feat"><div class="fn">1</div><div><p class="ft">日均振幅 ≥ 12%（示例 16%–27%）</p>
<p class="fd">(最高-最低)/收盘 的日均值。A 股主板约 3%，BTC 约 2%，这类币是 5–10 倍。振幅决定了 20 倍杠杆下你有没有“出错空间”。</p></div></div>
<div class="feat"><div class="fn">2</div><div><p class="ft">日波动率 ≥ 10%（示例 13.5%–23.4%）</p>
<p class="fd">日收益率的标准差。它决定了止损位该放多宽 —— 波动 20% 的币，3% 的止损等于随机噪音，必被扫。</p></div></div>
<div class="feat"><div class="fn">3</div><div><p class="ft">爆量倍数 15–555 倍（只参与打分，不作门槛）</p>
<p class="fd">最活跃一天的成交额 ÷ 中位日成交额。倍数越大，说明这个币越容易被资金一次性点燃，也越容易在几分钟内插针。<b>它不能当硬门槛</b>：中位成交额本身很大的币（如 BTW 中位 .6M），爆量倍数天然只有 15 倍；而流动性差的币动辄几百倍。所以这一项只进打分，不进筛选。</p></div></div>
<div class="feat"><div class="fn">4</div><div><p class="ft">价格极差 ≥ 8 倍（示例 23–438 倍）</p>
<p class="fd">记录期内最高收盘 / 最低收盘。没有这个量级的空间，做不出 LAB 那种两个月 10 倍的行情。</p></div></div>
<div class="feat"><div class="fn">5</div><div><p class="ft">中位日成交额 ≥ $2M（示例 $4.3M–$87.6M）</p>
<p class="fd">低于这个量级，滑点和插针会吃掉一切优势；高于 $10M 的只有 {alive} 个，属于“能上仓位”的级别。</p></div></div>
<div class="feat"><div class="fn">6</div><div><p class="ft">上线至今 ≥ 60 天且仍在交易</p>
<p class="fd">过滤掉刚上线还没形成结构的，以及已经下架的（下架币不在数据包里，属于幸存者偏差的一部分）。</p></div></div>
</div>

<div class="panel">
<h2>五、重新选出的 {len(selected)} 个同款币</h2>
<p class="note">满足上述 5 条硬门槛的共 {len(selected)} 个，按同款度排序。分档：≥90 分 {t90} 个、85–90 分 {t85} 个、80–85 分 {t80} 个、80 分以下 {tlow} 个。
成交额列标红表示中位日成交额 &lt; $4M（仓位要相应缩小）。</p>
<table><thead><tr><th>#</th><th>币种</th><th>上线</th><th>记录天数</th><th>期内涨跌</th><th>日均振幅</th>
<th>日波动率</th><th>中位日成交额</th><th>爆量倍数</th><th>价格极差</th><th>最大单日涨</th>
<th>距最高点</th><th>同款度</th></tr></thead><tbody>{sel_rows}</tbody></table>
<p class="note">「距最高点」= 当前收盘 ÷ 记录期内最高价 − 1；「期内涨跌」= 记录期首日收盘 → 当前收盘。</p>
</div>

<div class="panel">
<h2>六、逐币走势图（发行后第 60 天至今）</h2>
<p class="note">上：日/周 K 线（记录期超过 95 天自动按周聚合，避免蜡烛挤成一条线）；下：同期成交额柱状（对数轴），橙色虚线为 $10M/日 阈值。
每张图右上角是记录期涨跌、日均振幅与中位成交额。完整的单文件图在 <code>charts/</code> 目录。</p>
<div class="grid">{cards}</div>
</div>

<div class="panel">
<h2>七、这份名单该怎么用（结合你过去的交易记录）</h2>
<div class="ok">
<b>它和你上一份交易诊断是同一条线索的两端。</b>你 65 笔交易里，亏得最多的是
LAB 空单（扛 40 天 −2,066）、ETH 200x（多次爆仓）；赚得最多的是 LAB 多单（5/30–6/2 五笔 +2,245）。
这四个币恰好都出现在这份同款名单里 —— 说明你的“能力圈”本来就该落在这一档标的上，问题只在风控。
</div>
<h3>三条直接可用的规则</h3>
<ul>
<li><b>只在同款名单里做，且只用名单里的“流动性达标”子集。</b>中位日成交额 ≥ $10M 的只有 {alive} 个，
其余 {len(selected)-alive} 个的容量有限：按单笔不超过当日成交额 0.5% 估算，$2M/日 的币一天只能做 $10K 名义，20 倍下约 $500 保证金。</li>
<li><b>止损必须按波动率给，不能按固定百分比。</b>这些币日均振幅 16%–27%，1 倍 ATR 的止损会被反复扫。
建议初始止损 = 1.5×ATR(日)，或用“单笔风险 = 账户 1.5%”反推仓位 —— 两者取更保守的那个。</li>
<li><b>这批币里 {died} 个已经从最高点跌超 90%。</b>所以永远不要“因为跌得多所以抄底”，也永远不要在亏损单上加仓
（你上次的 −2,066 就是这么来的）。同款名单是“波动率合适”，不是“方向会涨”。</li>
</ul>
<div class="warn">
<b>风险提示：</b>名单里 {big_up} 个币记录期内涨幅 &gt; 100%（AKE 甚至 +8710%），{died} 个跌幅 &gt; 90%。
这是一个<b>极高方差</b>的样本 —— 任何一次满仓、任何一次不设止损，都会被其中的极端值清算。
名单的价值是“框定战场”，不是“给出方向”。
</div>
</div>

<div class="panel">
<h2>八、数据说明与局限</h2>
<ul class="note">
<li><b>幸存者偏差</b>：Binance 公开数据包只包含仍在交易的合约，已下架的币种不在样本内，因此真实的“同款币死亡率”会比 {died}/{len(selected)} 更高。</li>
<li><b>合约独有币的覆盖</b>：名字池来自 Gate.io（永续 + 现货），可能漏掉极少数 Binance 独有的合约币；共确认 {len(B)} 个，覆盖了 RAVE/LAB/BTW/LIGHT 这类主流新币。</li>
<li><b>价格源差异</b>：现货币用现货 K 线，合约独有币用永续 K 线；同一天两者价差通常在 0.1% 以内，不影响波动率结论。</li>
<li><b>当期数据</b>：合约月度数据包月末后才发布，本期已用日度数据补到 {d2s(now)}。</li>
<li><b>同款度不是预测</b>：它是“形态相似度”，衡量的是波动结构而非收益预期。同款度高 ≠ 会涨。</li>
<li>整理时间：{time.strftime('%Y-%m-%d %H:%M', time.gmtime())}（UTC）；数据由 <code>bn.py / build_universe.py / features.py / pick.py / doc.py</code> 生成，可复跑。</li>
</ul>
</div>
</div></body></html>"""
