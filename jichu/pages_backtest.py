# -*- coding: utf-8 -*-
"""
量化回测页面（双模式）
======================
模式一：单一个股——自选股票，按第五模块策略权重的时序多因子做择时，
        展示每日仓位、买卖价格/数量/原因与绩效；
模式二：全市场组合——全市场快照筛选股票池，定期多因子调仓，
        展示逐笔调仓、止损、行业分布与相对全市场等权的超额。

所有回测均为收盘生成信号、次日开盘成交，不使用未来数据。
"""
import datetime as dt
import re
import threading
from typing import Callable

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFrame, QProgressBar,
                               QComboBox, QStackedWidget, QTabWidget,
                               QCompleter, QDialog, QCheckBox, QTableWidget)
import pyqtgraph as pg

from ui_common import (UP, DOWN, FLAT, CARD, TEXT, TEXT2, BORDER, WARN,
                       Worker, make_table, cell, pct_color, skeleton_table,
                       SortableItem)
import backtest_service
import quant_lab
import strategy_library as SL
import tactics

pg.setConfigOption("background", CARD)
pg.setConfigOption("foreground", TEXT2)

# 图表页放在页签末尾，避免改动既有数据表的 0~4 索引
CHART_TAB = 5

# 股票池上限 → 选股方式说明（池越小越偏向龙头/大票，越大越接近全市场采样）
POOL_MODE = {
    20: ("龙头精选池",
         "只在成交额前 20 只中按多因子排序，候选项最少、信号最集中；"
         "相当于在大票里做龙头跟随，建议持仓 5 只"),
    50: ("核心活跃池",
         "成交额前 50 只内多因子排序，兼顾集中度与分散度，建议持仓 5~10 只"),
    100: ("主流宽基池",
         "成交额前 100 只是市场主线与二三线龙头的交集，建议持仓 10~20 只"),
    400: ("全市场流动性池",
         "剔除 ST/新股/低价/低成交额后按成交额取前 400 只，最接近全市场采样，"
         "建议持仓 20 只"),
}
# 每种池容量对应的默认持仓数（用户仍可手动改）
POOL_DEFAULT_TOP_N = {20: 5, 50: 10, 100: 20, 400: 20}


class DateAxis(pg.AxisItem):
    """把整数索引映射为日期字符串（MM-DD）。"""
    def __init__(self, dates):
        super().__init__(orientation="bottom")
        self.dates = dates

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(v)
            if 0 <= i < len(self.dates):
                out.append(self.dates[i][5:])
            else:
                out.append("")
        return out


class MetricCard(QFrame):
    def __init__(self, label):
        super().__init__()
        self.setObjectName("Card")
        self.setMinimumWidth(96)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        self.k = QLabel(label)
        self.k.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.v = QLabel("—")
        self.v.setStyleSheet("font-size:18px; font-weight:bold;")
        lay.addWidget(self.k)
        lay.addWidget(self.v)

    def set_val(self, text, color=None):
        self.v.setText(text)
        self.v.setStyleSheet(
            f"font-size:18px; font-weight:bold; color:{color or TEXT};")


class BacktestPage(QWidget):
    def __init__(self, set_status: Callable[[str], None]):
        super().__init__()
        self.set_status = set_status
        self._workers = []
        self._stock_worker = None
        self._stock_loaded = False
        self._cancel = None
        self._running = False
        self._build()

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # 参数栏
        bar = QFrame()
        bar.setObjectName("Card")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 10)
        bl.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.mode = QComboBox()
        self.mode.addItem("单一个股（择时）", "single")
        self.mode.addItem("全市场组合（多因子）", "universe")
        self.mode.addItem("每日荐股池（多因子荐股）", "pool")
        for t in tactics.TACTICS:
            self.mode.addItem(f"战法 · {t['name']}", f"tactic:{t['id']}")
        self.mode.setMinimumWidth(170)
        self.mode.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mode.setMinimumContentsLength(12)
        self.mode.currentIndexChanged.connect(self._on_mode)
        # 默认启动即「全市场组合（多因子）」；此处只切换索引，_on_mode 在构建末尾统一调用
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(1)
        self.mode.blockSignals(False)

        self.strategy = QComboBox()
        self.strategy.setMinimumWidth(220)
        self.strategy.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.strategy.setMinimumContentsLength(14)
        for s in SL.list_strategies():
            self.strategy.addItem(f"{s['org']}｜{s['name']}", s["id"])
        self.strategy.currentIndexChanged.connect(self._on_strategy)

        self.months = QComboBox()
        for m in (12, 18, 24):
            self.months.addItem(f"{m} 个月", m)
        self.months.setCurrentIndex(1)   # 默认 18 个月
        self.months.setFixedWidth(100)
        self.months.setToolTip("回测区间：以今天为终点向前取 N 个月的交易日")

        self.cash = QComboBox()
        for lab, v in [("5 万", 50_000), ("10 万", 100_000),
                       ("20 万", 200_000), ("100 万", 1_000_000)]:
            self.cash.addItem(lab, v)
        self.cash.setCurrentIndex(2)      # 默认 20 万
        self.cash.setFixedWidth(100)
        self.cash.setToolTip("回测初始资金，决定单笔买入的最小金额与仓位粒度")

        for lab, w in [("模式", self.mode), ("策略", self.strategy),
                       ("回测长度", self.months), ("初始资金", self.cash)]:
            row1.addWidget(QLabel(lab + "："))
            row1.addWidget(w)
        row1.addStretch()
        bl.addLayout(row1)

        # 第二行：开关 + 开始回测按钮（单独成行，避免被挤出可视区）
        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.neutral_cb = QCheckBox("行业/市值中性化")
        self.neutral_cb.setChecked(False)
        self.neutral_cb.setToolTip("全市场模式：先行业内排分位，再剔除市值暴露")
        self.exec_cb = QCheckBox("涨跌停/停牌约束")
        self.exec_cb.setChecked(False)
        self.exec_cb.setToolTip("涨停不追买、跌停不砸卖，并按成交量 5% 限额与冲击成本")
        self.fund_cb = QCheckBox("资金面增强（龙虎榜+北向+席位）")
        self.fund_cb.setChecked(False)
        self.fund_cb.setToolTip("龙虎榜净买额占流通市值 + 北向成交额占比，"
                               "并按上榜席位历史胜率调整，以 15% 权重叠加到总分")
        row2.addWidget(self.neutral_cb)
        row2.addWidget(self.exec_cb)
        row2.addWidget(self.fund_cb)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(130)
        self.progress.setVisible(False)
        row2.addWidget(self.progress)

        self.btn = QPushButton("开始回测")
        self.btn.setObjectName("Primary")
        self.btn.setFixedWidth(110)
        self.btn.clicked.connect(self.run)
        row2.addWidget(self.btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setFixedWidth(80)
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._stop)
        row2.addWidget(self.stop_btn)
        row2.addStretch()
        bl.addLayout(row2)

        # 模式专属参数
        self.params = QStackedWidget()
        single = QFrame()
        sl = QHBoxLayout(single)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)
        self.stock = QComboBox()
        self.stock.setEditable(True)
        self.stock.setMinimumWidth(230)
        self.stock.setInsertPolicy(QComboBox.NoInsert)
        self.stock.addItem("输入代码或选择股票…", "")
        self.stock.currentIndexChanged.connect(self._on_stock)
        self.stock_name = QLabel("—")
        self.stock_name.setStyleSheet(f"color:{TEXT2};")
        self.single_hint = QLabel(
            "自选个股 · 仓位随综合分变化 · 收盘信号次日开盘成交")
        self.single_hint.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.single_hint.setWordWrap(True)
        sl.addWidget(QLabel("股票："))
        sl.addWidget(self.stock)
        sl.addWidget(self.stock_name)
        sl.addWidget(self.single_hint)
        sl.addStretch()

        multi = QFrame()
        mv = QVBoxLayout(multi)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(4)
        mr = QHBoxLayout()
        mr.setSpacing(8)
        self.pool = QComboBox()
        for n in (20, 50, 100, 400):
            self.pool.addItem(f"{n} 只", n)
        self.pool.setCurrentIndex(2)      # 默认 100 只（主流宽基池）
        self.pool.setFixedWidth(92)
        self.pool.setToolTip("股票池上限：按成交额（流动性）从高到低取前 N 只，"
                             "不同池容量对应不同的选股思路")
        self.pool.currentIndexChanged.connect(self._on_pool_change)
        self.topn = QComboBox()
        for n in (5, 10, 20):
            self.topn.addItem(f"{n} 只", n)
        self.topn.setCurrentIndex(0)      # 默认 5 只
        self.topn.setFixedWidth(88)
        self.topn.setToolTip("同时持有的股票数量；数量越少越集中")
        self.rebal = QComboBox()
        for lab, v in [("每 5 个交易日", 5), ("每 10 个交易日", 10),
                       ("每 20 个交易日（月度）", 20)]:
            self.rebal.addItem(lab, v)
        self.rebal.setCurrentIndex(2)
        self.rebal.setToolTip("每 N 个交易日重算一次目标名单并换仓；"
                              "非调仓日只做止损，不会每天买卖")
        self.pool_pos = QComboBox()
        for p in (50, 60, 80, 100):
            self.pool_pos.addItem(f"{p} %", p)
        self.pool_pos.setCurrentIndex(3)  # 默认 100%
        self.pool_pos.setFixedWidth(88)
        self.pool_pos.setToolTip("组合总仓位上限；留出的现金用于加仓或应对回调")
        for lab, w in [("股票池上限", self.pool), ("持仓数", self.topn),
                       ("调仓周期", self.rebal), ("总仓位上限", self.pool_pos)]:
            mr.addWidget(QLabel(lab + "："))
            mr.addWidget(w)
        mr.addStretch()
        mv.addLayout(mr)

        mh = QHBoxLayout()
        mh.setSpacing(10)
        self.price_cb = QCheckBox("剔除股价 > 60 元")
        self.price_cb.setChecked(False)
        self.price_cb.setToolTip("勾选后从股票池剔除高价股（波动与资金占用较大）")
        self.star_cb = QCheckBox("排除科创板/北交所")
        self.star_cb.setChecked(True)
        self.star_cb.setToolTip(
            "剔除科创板（688/689）与北交所（8/4/9 开头）个股；"
            "资金体量不足、未开通相应权限时建议保持勾选")
        self.multi_hint = QLabel("")
        self.multi_hint.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.multi_hint.setWordWrap(True)
        mh.addWidget(self.price_cb)
        mh.addWidget(self.star_cb)
        mh.addWidget(self.multi_hint, 1)
        mv.addLayout(mh)
        # 首次只刷新说明文案，持仓数保持截图默认的 5 只（用户改池容量时再自动联动）
        self._on_pool_change(sync_topn=False)

        # 战法模式参数
        tactic = QFrame()
        tv = QVBoxLayout(tactic)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        tr = QHBoxLayout()
        tr.setSpacing(8)
        self.t_pool = QComboBox()
        for n in (20, 50, 100, 400):
            self.t_pool.addItem(f"{n} 只", n)
        self.t_pool.setCurrentIndex(3)    # 默认 400 只
        self.t_pool.setFixedWidth(92)
        self.t_pool.setToolTip("战法候选池：按成交额（流动性）从高到低取前 N 只")
        self.t_topn = QComboBox()
        for n in (5, 10, 20):
            self.t_topn.addItem(f"{n} 只", n)
        self.t_topn.setCurrentIndex(0)    # 默认 5 只
        self.t_topn.setFixedWidth(88)
        self.t_topn.setToolTip("同时持有的股票数量上限；数量越少越集中")
        self.t_pos = QComboBox()
        for p in (50, 60, 80, 100):
            self.t_pos.addItem(f"{p} %", p)
        self.t_pos.setCurrentIndex(2)     # 默认 80%
        self.t_pos.setFixedWidth(88)
        self.t_pos.setToolTip("总仓位上限；剩余现金可留给加仓机会")
        for lab, w in [("股票池上限", self.t_pool), ("持仓上限", self.t_topn),
                       ("总仓位上限", self.t_pos)]:
            tr.addWidget(QLabel(lab + "："))
            tr.addWidget(w)
        tr.addStretch()
        tv.addLayout(tr)

        th = QHBoxLayout()
        th.setSpacing(10)
        self.t_price_cb = QCheckBox("剔除股价 > 60 元")
        self.t_price_cb.setChecked(True)
        self.tactic_hint = QLabel("")
        self.tactic_hint.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.tactic_hint.setWordWrap(True)
        th.addWidget(self.t_price_cb)
        th.addWidget(self.tactic_hint, 1)
        tv.addLayout(th)

        self.params.addWidget(single)
        self.params.addWidget(multi)
        self.params.addWidget(tactic)
        bl.addWidget(self.params)
        root.addWidget(bar)

        self.strategy_lbl = QLabel("")
        self.strategy_lbl.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.strategy_lbl.setWordWrap(True)
        root.addWidget(self.strategy_lbl)

        # 指标卡片
        self.metrics = {}
        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        for key, lab in [("total_return", "总收益%"), ("annual_return", "年化%"),
                         ("excess_return", "超额%"), ("max_drawdown", "最大回撤%"),
                         ("sharpe", "夏普"), ("win_rate", "胜率%"),
                         ("profit_factor", "盈亏比"), ("trades", "平仓次数")]:
            c = MetricCard(lab)
            self.metrics[key] = c
            mrow.addWidget(c)
        root.addLayout(mrow)

        # 结果查看：上方选择器决定下方显示图表还是某张数据表
        view_row = QHBoxLayout()
        view_row.setSpacing(8)
        view_row.addWidget(QLabel("查看内容："))
        self.view_sel = QComboBox()
        self.view_sel.setMinimumWidth(220)
        self.view_sel.currentIndexChanged.connect(self._on_view_change)
        view_row.addWidget(self.view_sel)
        view_row.addStretch()
        self.pop_btn = QPushButton("放大查看")
        self.pop_btn.setObjectName("GhostBtn")
        self.pop_btn.setToolTip("在新窗口中放大查看当前选中的图表或数据表")
        self.pop_btn.clicked.connect(self._popup_current)
        view_row.addWidget(self.pop_btn)
        root.addLayout(view_row)

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(420)
        # 隐藏页签栏，统一用上方的“查看内容”下拉选择
        self.tabs.tabBar().setVisible(False)
        # 每日操作：末列"说明"保留拉伸填满，其余列压缩到合计约 634px，
        # 避免整表过宽导致默认就出现横向滚动条
        self.daily_tbl = make_table(
            ["日期", "操作", "代码", "名称", "价格", "数量(股)", "仓位%",
             "现金", "总资产", "说明"])
        for i, w in enumerate([88, 68, 64, 84, 64, 76, 60, 82, 86]):
            self.daily_tbl.setColumnWidth(i, w)
        # 交易明细：末列"原因"保留拉伸，其余列合计约 760px
        self.trade_tbl = make_table(
            ["日期", "代码", "名称", "方向", "价格", "数量(股)", "金额",
             "仓位%", "买入日期", "买入价", "盈亏", "原因"])
        for i, w in enumerate([88, 64, 84, 96, 64, 78, 86, 60, 86, 64, 86]):
            self.trade_tbl.setColumnWidth(i, w)
        # 持仓明细：列宽加宽，末列"浮动盈亏"拉伸填满剩余宽度
        self.pos_tbl = make_table(
            ["日期", "代码", "名称", "持股数", "价格", "市值",
             "权重%", "成本", "浮动盈亏"], stretch_last=True)
        for i, w in enumerate([88, 64, 96, 84, 64, 96, 64, 66, 96]):
            self.pos_tbl.setColumnWidth(i, w)
        self.diag_tbl = make_table(["维度", "指标", "数值", "说明"])
        for i, w in enumerate([80, 132, 96]):
            self.diag_tbl.setColumnWidth(i, w)
        # 收益归因：末列"交易次数"是数值，不拉伸
        self.attr_tbl = make_table(
            ["代码", "名称", "行业", "已实现盈亏", "贡献%", "胜率", "交易次数"],
            stretch_last=False)
        for i, w in enumerate([66, 96, 110, 100, 74, 66, 78]):
            self.attr_tbl.setColumnWidth(i, w)
        self.pos_note = QLabel("运行回测后，这里会说明最近一次调仓/止损操作")
        self.pos_note.setObjectName("Hint")
        self.pos_note.setWordWrap(True)
        pos_page = QWidget()
        pv = QVBoxLayout(pos_page)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(6)
        pv.addWidget(self.pos_note)
        pv.addWidget(self.pos_tbl, 1)
        self.tabs.addTab(self.daily_tbl, "每日操作")
        self.tabs.addTab(self.trade_tbl, "交易明细")
        self.tabs.addTab(pos_page, "持仓明细")
        self.tabs.addTab(self.diag_tbl, "风险诊断")
        self.tabs.addTab(self.attr_tbl, "收益归因")

        # 图表页：净值、回撤、仓位三张图放在一起
        self.chart_page = QWidget()
        cl = QVBoxLayout(self.chart_page)
        cl.setContentsMargins(0, 0, 0, 0)
        self.chart_hint = QLabel("运行回测后，在此显示净值、回撤与仓位曲线")
        self.chart_hint.setStyleSheet(f"color:{TEXT2}; font-size:13px;")
        self.chart_hint.setAlignment(Qt.AlignCenter)
        self.gw = pg.GraphicsLayoutWidget()
        self.gw.setFrameShape(QFrame.StyledPanel)
        self.gw.setStyleSheet(f"border:1px solid {BORDER}; border-radius:6px;")
        self.gw.setVisible(False)
        cl.addWidget(self.chart_hint, 1)
        cl.addWidget(self.gw, 1)
        self.tabs.addTab(self.chart_page, "净值 / 仓位图")

        root.addWidget(self.tabs, stretch=1)
        self._popups = []
        skeleton_table(self.daily_tbl, 5, "选择模式后点击“开始回测”")
        skeleton_table(self.trade_tbl, 5, "交易价格、仓位与原因将在此展示")
        skeleton_table(self.pos_tbl, 5, "全市场模式的调仓日持仓将在此展示")
        skeleton_table(self.diag_tbl, 5, "风险与稳健性指标将在此展示")
        skeleton_table(self.attr_tbl, 5, "按个股与行业的已实现盈亏归因")

        # 默认展示图表页
        self.tabs.setCurrentIndex(CHART_TAB)
        self._rebuild_view_selector()

        self._on_mode()
        self._on_strategy()

    def _rebuild_view_selector(self):
        """按当前可见页签重建“查看内容”下拉项，保留原选中项。"""
        cur = self.tabs.currentIndex()
        self.view_sel.blockSignals(True)
        self.view_sel.clear()
        if self.tabs.isTabVisible(CHART_TAB):
            self.view_sel.addItem(self.tabs.tabText(CHART_TAB), CHART_TAB)
        for i in range(self.tabs.count()):
            if i != CHART_TAB and self.tabs.isTabVisible(i):
                self.view_sel.addItem(self.tabs.tabText(i), i)
        pos = self.view_sel.findData(cur)
        self.view_sel.setCurrentIndex(pos if pos >= 0 else 0)
        self.view_sel.blockSignals(False)
        self._on_view_change()

    def _on_view_change(self, _=None):
        idx = self.view_sel.currentData()
        if idx is not None:
            self.tabs.setCurrentIndex(int(idx))

    def _on_pool_change(self, _=None, sync_topn: bool = True):
        """股票池容量变化：同步更新选股方式说明，并给出匹配的默认持仓数。"""
        n = int(self.pool.currentData() or 100)
        name, desc = POOL_MODE.get(n, POOL_MODE[100])
        self.multi_hint.setText(
            f"选股方式：{name}（{n} 只）—— {desc}；调仓日按多因子排名生成目标名单，"
            "次日开盘等权建仓，非调仓日只做止损")
        if not sync_topn:
            return
        idx = self.topn.findData(POOL_DEFAULT_TOP_N.get(n, 20))
        if idx >= 0:
            self.topn.setCurrentIndex(idx)

    def _on_mode(self):
        data = self.mode.currentData() or "single"
        if data == "single":
            idx = 0
        elif data.startswith("tactic:"):
            idx = 2
        else:
            idx = 1
        self.params.setCurrentIndex(idx)
        if data == "pool":
            # 每日荐股池：默认更贴近“每天出表、短期换仓”的节奏
            self.rebal.setCurrentIndex(0)
        if idx == 2:
            t = tactics.get_tactic(data.split(":", 1)[1])
            pos = self.t_topn.findData(int(t["top_n"]))
            if pos >= 0:
                self.t_topn.setCurrentIndex(pos)
            self.tactic_hint.setText(t["desc"])
        if hasattr(self, "tabs"):
            # 持仓明细仅在组合 / 战法模式下有意义
            self.tabs.setTabVisible(2, idx != 0)
            self._rebuild_view_selector()
        if hasattr(self, "strategy_lbl"):
            if idx == 2:
                self.strategy_lbl.setText(
                    "战法按公开流传规则做规则化近似：收盘生成信号、次日开盘成交，"
                    "已计入佣金、印花税与滑点，不使用未来数据。")
            else:
                self._on_strategy()

    def _popup_current(self):
        """放大查看：图表页弹大图，数据表页复制表格到独立窗口。"""
        if self.tabs.currentIndex() == CHART_TAB:
            self._popup_chart()
        else:
            self._popup_current_table()

    def _popup_chart(self):
        data = getattr(self, "_curve_data", None)
        if not data:
            self.set_status("还没有回测图表，请先运行回测")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("回测图表 · 净值 / 回撤 / 仓位（放大）")
        dlg.resize(1400, 900)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(8, 8, 8, 8)
        gw = pg.GraphicsLayoutWidget()
        self._render_curves(gw, *data)
        lay.addWidget(gw)
        self._popups.append(dlg)
        dlg.show()

    # ---------------- 实时行情（全局被动刷新，3 秒） ----------------
    def live_codes(self):
        return list(getattr(self, "_live_hint", {}) or {})

    def apply_live(self, quotes: dict):
        """只刷新最新持仓的价格/市值/浮动盈亏，不重跑回测。"""
        hint = getattr(self, "_live_hint", None) or {}
        last_pd = getattr(self, "_live_date", "")
        if not hint or not quotes:
            return
        for row in range(self.pos_tbl.rowCount()):
            dit = self.pos_tbl.item(row, 0)
            cit = self.pos_tbl.item(row, 1)
            if dit is None or cit is None or dit.text() != last_pd:
                continue
            q = quotes.get(cit.text())
            h = hint.get(cit.text())
            if not q or not q.get("price") or not h:
                continue
            px = float(q["price"])
            shares, cost = h["shares"], h["cost"]
            val = shares * px
            pnl = val - cost * shares
            c4 = self.pos_tbl.item(row, 4)
            if c4 is not None:
                c4.setText(f"{px:.2f}")
            c5 = self.pos_tbl.item(row, 5)
            if c5 is not None:
                c5.setText(f"{val:,.0f}")
            c8 = self.pos_tbl.item(row, 8)
            if c8 is not None:
                c8.setText(f"{pnl:+,.0f}")
                c8.setForeground(QColor(pct_color(pnl)))

    def _render_pos_note(self, r):
        """持仓明细页顶部：说明最新一次持仓操作（时点、买卖、止盈/止损、继续持有）。"""
        def tag_name(t):
            return f"{t.get('name') or t['code']}({t['code']})"

        def hold_tag(rp):
            if rp >= 20:
                return f"<span style='color:{UP}'>浮盈 {rp:+.1f}%，可考虑止盈</span>"
            if rp <= -8:
                return f"<span style='color:{DOWN}'>浮亏 {rp:+.1f}%，接近止损线</span>"
            return f"<span style='color:{TEXT2}'>浮动 {rp:+.1f}%，继续持有</span>"

        trades = [t for t in (r.get("trades") or [])
                  if t.get("side") in ("买入", "卖出")]
        pos = r.get("positions") or []
        lines = ["<b>操作时点</b>：调仓日收盘计算目标名单 → <b>次日开盘</b>成交；"
                 "非调仓日只在收盘检查止损（成本回撤 &gt; 12%）→ 次日开盘卖出"]

        if trades:
            last = max(t.get("date", "") for t in trades)
            buys = [t for t in trades
                    if t.get("date") == last and t.get("side") == "买入"]
            sells = [t for t in trades
                     if t.get("date") == last and t.get("side") == "卖出"]
            seg = [f"<b>最新操作</b>：{last} 开盘执行"]
            if buys:
                seg.append("买入 " + "、".join(tag_name(t) for t in buys[:8]))
            if sells:
                sl = [t for t in sells if "止损" in (t.get("reason") or "")]
                tp = [t for t in sells
                      if t not in sl and (t.get("pnl") or 0) > 0]
                other = [t for t in sells if t not in sl and t not in tp]
                chunks = []
                if tp:
                    chunks.append("止盈 " + "、".join(tag_name(t) for t in tp[:8]))
                if sl:
                    chunks.append("止损 " + "、".join(tag_name(t) for t in sl[:8]))
                if other:
                    chunks.append("换仓卖出 " + "、".join(
                        tag_name(t) for t in other[:8]))
                seg.append("卖出 " + "；".join(chunks))
            if not buys and not sells:
                seg.append("该交易日无成交（仅持仓观望）")
            lines.append("；".join(seg) + "。")

        if pos:
            last_pd = max(p.get("date", "") for p in pos)
            held = [p for p in pos if p.get("date") == last_pd]
            self._live_date = last_pd
            self._live_hint = {
                str(p.get("code")): {"shares": float(p.get("shares") or 0),
                                     "cost": float(p.get("cost") or 0)}
                for p in held}
            if held:
                items = []
                for p in held[:8]:
                    cost = float(p.get("cost") or 0)
                    px = float(p.get("price") or 0)
                    rp = (px / cost - 1) * 100 if cost else 0.0
                    rp = round(rp, 1)
                    items.append(f"{p.get('name') or p['code']}"
                                 f"（{hold_tag(rp)}）")
                more = f" 等 {len(held)} 只" if len(held) > 8 else ""
                lines.append(f"<b>当前持仓</b>（{last_pd}）：" + "、".join(items)
                             + more)
        self.pos_note.setText("<br>".join(lines))

    def _render_curves(self, widget, dates, nav, bench, dd, pos, bench_label):
        """在指定图表控件中绘制净值、回撤、仓位三张图。"""
        widget.clear()
        x = np.arange(len(dates))
        ax = DateAxis(dates)
        p1 = widget.addPlot(row=0, col=0, axisItems={"bottom": ax})
        p1.plot(x, nav, pen=pg.mkPen(UP, width=2), name="策略净值")
        p1.plot(x, bench, pen=pg.mkPen(FLAT, width=1.4, style=Qt.DashLine),
                name=bench_label)
        p1.showGrid(x=True, y=True, alpha=0.25)
        p1.setLabel("left", "净值（归一化）")
        p1.addLegend(offset=(-10, 10))

        p2 = widget.addPlot(row=1, col=0)
        p2.plot(x, dd, pen=pg.mkPen(DOWN, width=1),
                fillLevel=0, fillBrush=pg.mkBrush(224, 51, 46, 60))
        p2.showGrid(x=True, y=True, alpha=0.25)
        p2.setLabel("left", "回撤%")

        p3 = widget.addPlot(row=2, col=0)
        p3.plot(x, pos, pen=pg.mkPen("#2E6FD6", width=1.4),
                fillLevel=0, fillBrush=pg.mkBrush(46, 111, 214, 40))
        p3.showGrid(x=True, y=True, alpha=0.25)
        p3.setLabel("left", "仓位%")
        p3.setYRange(0, 105)
        for p in (p1, p2):
            p.getAxis("bottom").setStyle(showValues=False)
        p2.setXLink(p3)
        p1.setXLink(p3)

    def _popup_current_table(self):
        """把当前页签的表格复制到一个独立大窗口，便于查看长周期数据。"""
        src = self.tabs.currentWidget()
        if src is not None and not isinstance(src, QTableWidget):
            src = src.findChild(QTableWidget)
        idx = self.tabs.currentIndex()
        if src is None or src.rowCount() == 0:
            self.set_status("当前表还没有数据，请先运行回测")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"回测结果 · {self.tabs.tabText(idx)}")
        dlg.resize(1180, 760)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(8, 8, 8, 8)
        headers = [src.horizontalHeaderItem(c).text()
                   for c in range(src.columnCount())]
        tbl = make_table(headers)
        tbl.setSortingEnabled(False)
        tbl.setRowCount(src.rowCount())
        for r_ in range(src.rowCount()):
            for c_ in range(src.columnCount()):
                it = src.item(r_, c_)
                if it is None:
                    continue
                ni = SortableItem(it.text(),
                                  getattr(it, "_sort_value", None))
                ni.setTextAlignment(it.textAlignment())
                ni.setForeground(it.foreground())
                if it.font().bold():
                    nf = ni.font()
                    nf.setBold(True)
                    ni.setFont(nf)
                tbl.setItem(r_, c_, ni)
        for c_ in range(src.columnCount()):
            tbl.setColumnWidth(c_, max(72, src.columnWidth(c_)))
        tbl.setSortingEnabled(True)
        lay.addWidget(tbl)
        self._popups.append(dlg)
        dlg.show()

    def _on_strategy(self):
        s = SL.get_strategy(self.strategy.currentData() or "high_flyer")
        names = SL.FACTOR_NAME
        weights = " · ".join(
            f"{names[k]} {round(v, 1)}" for k, v in s["weights"].items()
            if v)
        self.strategy_lbl.setText(
            f"策略近似：{s['org']}｜{s['name']}　因子满分：{weights}　"
            f"（公开方法论近似，非机构真实参数）")

    def _on_stock(self):
        code = self.stock.currentData()
        text = self.stock.currentText()
        if not code:
            m = re.search(r"(\d{6})", text)
            code = m.group(1) if m else ""
        name = ""
        if code and " " in text and text.split(" ", 1)[0] == code:
            name = text.split(" ", 1)[1]
        self.stock_name.setText(f"{code} {name}".strip() if code else "—")

    # ---------------- 异步加载股票列表 ----------------
    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_stocks()

    def _ensure_stocks(self):
        if self._stock_loaded or self._stock_worker:
            return
        from data_sources import fetch_all_quotes
        w = Worker(lambda p: fetch_all_quotes())
        w.done.connect(self._fill_stocks)
        w.error.connect(lambda m: self.stock_name.setText("股票列表加载失败，可直接输入代码"))
        w.finished.connect(self._stock_finished)
        self._stock_worker = w
        self._workers.append(w)
        w.start()

    def _stock_finished(self):
        self._stock_worker = None
        self._workers = [x for x in self._workers if x.isRunning()]

    def _fill_stocks(self, quotes):
        self._stock_loaded = True
        try:
            rows = sorted(quotes.values(), key=lambda q: q.get("code", ""))
            self.stock.blockSignals(True)
            self.stock.clear()
            for q in rows:
                code, name = q.get("code", ""), q.get("name", "")
                if code:
                    self.stock.addItem(f"{code} {name}", code)
            idx = self.stock.findData("601566")
            if idx >= 0:
                self.stock.setCurrentIndex(idx)
            else:
                self.stock.setCurrentIndex(-1)
                self.stock.setEditText("601566")
            self.stock.blockSignals(False)
            comp = self.stock.completer()
            if comp:
                comp.setCompletionMode(QCompleter.PopupCompletion)
                comp.setFilterMode(Qt.MatchContains)
            self._on_stock()
        except Exception:
            self.stock_name.setText("股票列表加载失败，可直接输入代码")

    # ---------------- 运行 ----------------
    def _parse_code(self) -> str:
        data = self.stock.currentData()
        if data:
            return str(data)
        m = re.search(r"(\d{6})", self.stock.currentText())
        return m.group(1) if m else ""

    def run(self):
        if self._running:
            return
        try:
            months = int(self.months.currentData())
            init_cash = float(self.cash.currentData() or 200_000)
            if init_cash <= 0:
                raise ValueError
        except Exception:
            self.set_status("初始资金或回测长度填写有误")
            return
        mode = self.mode.currentData()
        sid = self.strategy.currentData() or "high_flyer"
        if mode == "single":
            code = self._parse_code()
            if not re.match(r"^\d{6}$", code):
                self.set_status("请输入 6 位股票代码，例如 601566")
                return
            params = dict(code=code, months=months, init_cash=init_cash,
                          strategy_id=sid,
                          exec_constraints=self.exec_cb.isChecked(),
                          fund_boost=self.fund_cb.isChecked())
            fn = backtest_service.run_single_stock
            desc = f"单票回测 {code}"
        elif mode and mode.startswith("tactic:"):
            tid = mode.split(":", 1)[1]
            params = dict(tactic_id=tid, months=months, init_cash=init_cash,
                          top_n=int(self.t_topn.currentData()),
                          pool_size=int(self.t_pool.currentData()),
                          max_position=int(self.t_pos.currentData()) / 100,
                          max_price=60.0 if self.t_price_cb.isChecked() else None)
            fn = tactics.run_tactic
            desc = f"战法回测 · {tactics.get_tactic(tid)['name']}"
        else:
            params = dict(months=months, init_cash=init_cash,
                          strategy_id=sid, top_n=int(self.topn.currentData()),
                          pool_size=int(self.pool.currentData()),
                          rebalance_days=int(self.rebal.currentData() or 20),
                          neutralize=self.neutral_cb.isChecked(),
                          exec_constraints=self.exec_cb.isChecked(),
                          fund_boost=self.fund_cb.isChecked(),
                          max_position=int(self.pool_pos.currentData()) / 100,
                          max_price=60.0 if self.price_cb.isChecked() else None,
                          exclude_star_bse=self.star_cb.isChecked())
            if mode == "pool":
                def fn(progress=None, cancel=None):
                    r = backtest_service.run_universe(
                        **params, progress=progress, cancel=cancel)
                    # 每日荐股池：调仓日的多因子排名即当天的“荐股表”
                    r["mode_name"] = "每日荐股池·多因子荐股"
                    r["pool_mode"] = True
                    return r
                desc = f"每日荐股池回测（池上限 {params['pool_size']} 只）"
            else:
                fn = backtest_service.run_universe
                desc = f"全市场组合回测（池上限 {params['pool_size']} 只）"

        self._cancel = threading.Event()
        self._running = True
        self.btn.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.progress.setVisible(True)
        self.set_status(f"开始{desc}…")
        skeleton_table(self.daily_tbl, 5, "回测运行中…")
        self.pos_note.setText("回测运行中…最近一次调仓/止损操作将在持仓明细页顶部说明")
        skeleton_table(self.trade_tbl, 5, "回测运行中…")
        skeleton_table(self.diag_tbl, 5, "回测运行中…")
        skeleton_table(self.attr_tbl, 5, "回测运行中…")
        self.chart_hint.setText("回测运行中…")
        self.chart_hint.setVisible(True)
        self.gw.setVisible(False)
        self._curve_data = None

        w = Worker(lambda p: fn(**params, progress=p,
                                cancel=self._cancel))
        w.progress.connect(self.set_status)
        w.done.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_finished)
        self._workers.append(w)
        w.start()

    def _stop(self):
        if self._cancel is not None:
            self._cancel.set()
            self.set_status("正在停止回测…")

    def _on_error(self, msg):
        if "InterruptedError" in msg or "已取消" in msg:
            self.set_status("回测已取消")
        else:
            self.set_status("回测失败：" + msg)
        self._on_finished()

    def _on_finished(self):
        self._running = False
        self.btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.progress.setVisible(False)
        self._workers = [x for x in self._workers if x.isRunning()]

    # ---------------- 渲染 ----------------
    def _on_done(self, r):
        curve = r.get("curve") or []
        if not curve:
            self.set_status("回测完成，但区间内没有可用行情数据")
            skeleton_table(self.daily_tbl, 5, "区间内没有可用行情数据")
            skeleton_table(self.trade_tbl, 5, "区间内没有可用行情数据")
            skeleton_table(self.pos_tbl, 5, "区间内没有可用行情数据")
            skeleton_table(self.diag_tbl, 5, "区间内没有可用行情数据")
            skeleton_table(self.attr_tbl, 5, "区间内没有可用行情数据")
            self._curve_data = None
            self.gw.setVisible(False)
            self.chart_hint.setText("区间内没有可用行情数据")
            self.chart_hint.setVisible(True)
            self._on_finished()
            return

        m = r["metrics"]
        self.metrics["total_return"].set_val(
            f"{m['total_return']:.2f}", pct_color(m["total_return"]))
        self.metrics["annual_return"].set_val(
            f"{m['annual_return']:.2f}", pct_color(m["annual_return"]))
        self.metrics["excess_return"].set_val(
            f"{m['excess_return']:+.2f}", pct_color(m["excess_return"]))
        self.metrics["max_drawdown"].set_val(
            f"{m['max_drawdown']:.2f}", DOWN)
        self.metrics["sharpe"].set_val(
            "—" if m["sharpe"] is None else f"{m['sharpe']:.2f}")
        self.metrics["win_rate"].set_val(
            "—" if m["win_rate"] is None else f"{m['win_rate']:.1f}")
        pf = m.get("profit_factor")
        self.metrics["profit_factor"].set_val(
            "—" if pf is None else f"{pf:.2f}",
            pct_color((pf - 1) * 100) if pf is not None else None)
        self.metrics["trades"].set_val(f"{m['trades']}")

        # 净值 / 回撤 / 仓位
        dates = [c["date"] for c in curve]
        nav = [c["nav"] / r["init_cash"] for c in curve]
        bench = [c["bench"] / r["init_cash"] for c in curve]
        peak = -1e18
        dd = []
        for c in curve:
            peak = max(peak, c["nav"])
            dd.append((c["nav"] / peak - 1) * 100 if peak else 0.0)
        pos = [c["position_pct"] for c in curve]

        self._curve_data = (dates, nav, bench, dd, pos,
                            r.get("bench_label", "基准"))
        self.chart_hint.setVisible(False)
        self.gw.setVisible(True)
        self._render_curves(self.gw, *self._curve_data)

        # 每日操作
        t = self.daily_tbl
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for d in r["daily"]:
            rr = t.rowCount()
            t.insertRow(rr)
            t.setItem(rr, 0, cell(d["date"], TEXT2, align=Qt.AlignCenter))
            ac = d.get("action", "")
            if "买入" in ac and "卖出" in ac:
                ac_c = WARN
            elif "买入" in ac:
                ac_c = UP
            elif "卖出" in ac:
                ac_c = DOWN
            else:
                ac_c = FLAT
            t.setItem(rr, 1, cell(ac, ac_c, bold=True,
                                   align=Qt.AlignCenter))
            t.setItem(rr, 2, cell(d.get("code") or "—", align=Qt.AlignCenter))
            t.setItem(rr, 3, cell(d.get("name") or "—", align=Qt.AlignCenter))
            price = d.get("price")
            t.setItem(rr, 4, cell("—" if price is None else f"{price:.2f}",
                                   align=Qt.AlignRight | Qt.AlignVCenter,
                                   sort_value=price if price is not None else -1))
            sh = d.get("shares")
            t.setItem(rr, 5, cell("—" if sh is None else f"{sh:,}",
                                   align=Qt.AlignRight | Qt.AlignVCenter,
                                   sort_value=sh if sh is not None else -1))
            t.setItem(rr, 6, cell(f"{d.get('position_pct', 0):.1f}",
                                   "#2E6FD6", align=Qt.AlignRight | Qt.AlignVCenter,
                                   sort_value=d.get("position_pct", 0)))
            t.setItem(rr, 7, cell(f"{d.get('cash', 0):,.0f}",
                                   align=Qt.AlignRight | Qt.AlignVCenter,
                                   sort_value=d.get("cash", 0)))
            t.setItem(rr, 8, cell(f"{d.get('total', 0):,.0f}", bold=True,
                                   align=Qt.AlignRight | Qt.AlignVCenter,
                                   sort_value=d.get("total", 0)))
            t.setItem(rr, 9, cell(d.get("note", "")))
        t.setSortingEnabled(True)

        # 交易明细
        tt = self.trade_tbl
        tt.setSortingEnabled(False)
        tt.setRowCount(0)
        for d in r["trades"]:
            rr = tt.rowCount()
            tt.insertRow(rr)
            side = d.get("side", "")
            sc = UP if side == "买入" else (DOWN if side == "卖出" else WARN)
            tt.setItem(rr, 0, cell(d.get("date", ""), TEXT2,
                                    align=Qt.AlignCenter))
            tt.setItem(rr, 1, cell(d.get("code", ""), align=Qt.AlignCenter))
            tt.setItem(rr, 2, cell(d.get("name", ""), align=Qt.AlignCenter))
            tt.setItem(rr, 3, cell(side, sc, bold=True, align=Qt.AlignCenter))
            tt.setItem(rr, 4, cell(f"{d.get('price', 0):.2f}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=d.get("price", 0)))
            tt.setItem(rr, 5, cell(f"{d.get('shares', 0):,}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=d.get("shares", 0)))
            tt.setItem(rr, 6, cell(f"{d.get('amount', 0):,.0f}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=d.get("amount", 0)))
            pct = d.get("position_pct")
            tt.setItem(rr, 7, cell("—" if pct is None else f"{pct:.1f}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=pct if pct is not None else -1))
            tt.setItem(rr, 8, cell(d.get("buy_date") or "—",
                                    align=Qt.AlignCenter))
            bp = d.get("buy_price")
            tt.setItem(rr, 9, cell("—" if bp is None else f"{bp:.2f}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=bp if bp is not None else -1))
            pnl = d.get("pnl")
            tt.setItem(rr, 10, cell("—" if pnl is None else f"{pnl:+,.0f}",
                                     pct_color(pnl) if pnl is not None else FLAT,
                                     align=Qt.AlignRight | Qt.AlignVCenter,
                                     sort_value=pnl if pnl is not None else 0))
            tt.setItem(rr, 11, cell(d.get("reason", "")))
        tt.setSortingEnabled(True)

        # 持仓明细（全市场模式：调仓日与期末的每只持仓）
        pos = r.get("positions") or []
        pt = self.pos_tbl
        pt.setSortingEnabled(False)
        pt.setRowCount(0)
        for d in pos:
            rr = pt.rowCount()
            pt.insertRow(rr)
            pt.setItem(rr, 0, cell(d.get("date", ""), TEXT2,
                                    align=Qt.AlignCenter))
            pt.setItem(rr, 1, cell(d.get("code", ""), align=Qt.AlignCenter))
            pt.setItem(rr, 2, cell(d.get("name", ""), align=Qt.AlignCenter))
            pt.setItem(rr, 3, cell(f"{d.get('shares', 0):,}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=d.get("shares", 0)))
            pt.setItem(rr, 4, cell(f"{d.get('price', 0):.2f}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=d.get("price", 0)))
            pt.setItem(rr, 5, cell(f"{d.get('value', 0):,.0f}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=d.get("value", 0)))
            w = d.get("weight", 0)
            pt.setItem(rr, 6, cell(f"{w:.1f}", "#2E6FD6",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=w))
            pt.setItem(rr, 7, cell(f"{d.get('cost', 0):.2f}",
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=d.get("cost", 0)))
            pnl = d.get("pnl")
            pt.setItem(rr, 8, cell("—" if pnl is None else f"{pnl:+,.0f}",
                                    pct_color(pnl) if pnl is not None else FLAT,
                                    align=Qt.AlignRight | Qt.AlignVCenter,
                                    sort_value=pnl if pnl is not None else 0))
        pt.setSortingEnabled(True)
        self._render_pos_note(r)

        # 风险诊断：风险分析 + 稳健性检验 + 交易成本
        es = r.get("exec_stats") or {}

        def fnum(v, suffix="", signed=False, digits=2):
            if v is None:
                return "—"
            t = f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"
            return t + suffix

        diag_items = [
            ("风险", "年化波动率", fnum(m.get("volatility"), "%"), TEXT,
             "净值日收益的年化标准差，衡量整体波动水平"),
            ("风险", "Sortino 比率", fnum(m.get("sortino")), TEXT,
             "只惩罚下行波动的收益风险比，越高越好"),
            ("风险", "Calmar 比率", fnum(m.get("calmar")), TEXT,
             "年化收益 / 最大回撤，衡量回撤修复能力"),
            ("风险", "VaR(95%)", fnum(m.get("var95"), "%"), TEXT,
             "历史日收益 5% 分位，单日极端亏损参考"),
            ("稳健性", "月度胜率", fnum(m.get("monthly_win_rate"), "%"), TEXT,
             "月度收益为正的比例，检验收益是否稳定"),
            ("稳健性", "最好月", fnum(m.get("best_month"), "%", signed=True),
             pct_color(m.get("best_month")),
             "单月最大收益，观察收益是否过度集中于某月"),
            ("稳健性", "最差月", fnum(m.get("worst_month"), "%", signed=True),
             pct_color(m.get("worst_month")),
             "单月最大亏损，观察极端月份的风险暴露"),
            ("稳健性", "前半段收益",
             fnum(m.get("first_half_return"), "%", signed=True),
             pct_color(m.get("first_half_return")),
             "样本前一半区间收益，检验是否只靠单段行情"),
            ("稳健性", "后半段收益",
             fnum(m.get("second_half_return"), "%", signed=True),
             pct_color(m.get("second_half_return")),
             "样本后一半区间收益，与前半段对比判断衰减"),
            ("交易", "平均仓位", fnum(m.get("avg_position"), "%"), TEXT,
             "回测期平均持仓比例，反映资金利用率"),
            ("交易", "累计换手", fnum(m.get("turnover"), " 倍"), TEXT,
             "仓位变化绝对值累计，越高说明调仓越频繁"),
            ("交易", "费用占初始资金", fnum(m.get("fees_pct"), "%"), TEXT,
             "佣金 + 印花税 + 滑点合计成本占比"),
            ("交易", "交易日数", f"{m.get('days', 0)} 天", TEXT,
             "回测区间内的实际交易日数量"),
            ("执行", "未成交委托", f"{es.get('blocked_buy', 0)} 买 / "
                                    f"{es.get('blocked_sell', 0)} 卖", TEXT,
             "涨跌停或停牌导致无法成交的委托笔数"),
            ("执行", "成交量限额", f"{es.get('capped_buy', 0)} 笔", TEXT,
             "单票单日超过成交量 5% 被自动缩减的买单"),
            ("执行", "冲击成本", f"{es.get('impact_cost', 0):,.0f} 元", TEXT,
             "按订单占当日成交额比例估算的额外成本"),
            ("资金面", "龙虎榜/北向/席位增强",
             f"{'开' if r.get('fund_boost') else '关'}"
             f"（权重 {r.get('fund_weight', 0):.0%}）", TEXT,
             "龙虎榜净买额占流通市值 + 北向成交额占比，"
             "并按席位历史胜率（3日上涨概率）放大或缩小"),
        ]
        dt = self.diag_tbl
        dt.setSortingEnabled(False)
        dt.setRowCount(0)
        for dim, name, val, color, desc in diag_items:
            rr = dt.rowCount()
            dt.insertRow(rr)
            dt.setItem(rr, 0, cell(dim, TEXT2, align=Qt.AlignCenter))
            dt.setItem(rr, 1, cell(name))
            dt.setItem(rr, 2, cell(val, color, bold=True,
                                   align=Qt.AlignRight | Qt.AlignVCenter))
            dt.setItem(rr, 3, cell(desc, TEXT2))

        # 收益归因：按个股统计已实现盈亏与贡献，并写入历史记录
        try:
            att = quant_lab.attribution(r)
            at = self.attr_tbl
            at.setSortingEnabled(False)
            at.setRowCount(0)
            for a in att["stocks"]:
                rr = at.rowCount()
                at.insertRow(rr)
                at.setItem(rr, 0, cell(a["code"], align=Qt.AlignCenter))
                at.setItem(rr, 1, cell(a["name"]))
                at.setItem(rr, 2, cell(a["industry"], TEXT2))
                at.setItem(rr, 3, cell(f"{a['pnl']:+,.0f}",
                                        pct_color(a["pnl"]),
                                        align=Qt.AlignRight | Qt.AlignVCenter,
                                        sort_value=a["pnl"]))
                at.setItem(rr, 4, cell(f"{a['contribution']:+.2f}%",
                                        pct_color(a["contribution"]),
                                        align=Qt.AlignCenter,
                                        sort_value=a["contribution"]))
                at.setItem(rr, 5, cell(f"{a['win_rate']:.1f}%",
                                        align=Qt.AlignCenter))
                at.setItem(rr, 6, cell(str(a["trades"]), TEXT2,
                                        align=Qt.AlignCenter))
            at.setSortingEnabled(True)
            if at.rowCount() == 0:
                skeleton_table(self.attr_tbl, 3, "没有已实现的卖出交易")
            self.tabs.setTabText(4, f"收益归因（{len(att['stocks'])} 只）")
            try:
                quant_lab.log_backtest(r, att=att)
            except Exception:
                pass
        except Exception:
            skeleton_table(self.attr_tbl, 3, "归因计算失败（行情快照不可用）")

        self.tabs.setTabText(0, f"每日操作（{len(r['daily'])}）")
        self.tabs.setTabText(1, f"交易明细（{len(r['trades'])}）")
        self.tabs.setTabText(2, f"持仓明细（{len(pos)}）")
        self.tabs.setTabText(3, f"风险诊断（{len(diag_items)}）")
        self.tabs.setTabVisible(2, r.get("mode") != "single")
        # 页签标题变化（条数）后同步刷新下拉选择器
        self._rebuild_view_selector()

        extra = ""
        if r.get("mode") == "universe":
            extra = (f"｜全市场 {r.get('universe_total')} 只 → 筛选 "
                     f"{r.get('pool_selected')} 只 → 成功加载 "
                     f"{r.get('loaded')} 只")
        elif r.get("mode") == "tactic":
            extra = (f"｜网络热门战法（规则化近似）｜持仓上限 "
                     f"{r.get('top_n')} 只｜成功加载 {r.get('loaded')} 只")
        self.set_status(
            f"{r['mode_name']}｜{r['strategy_org']} {r['strategy_name']}｜"
            f"{r['start']} ~ {r['end']}｜总收益 {m['total_return']:.2f}%｜"
            f"超额 {m['excess_return']:+.2f}%｜胜率 "
            f"{'—' if m['win_rate'] is None else str(m['win_rate']) + '%'}｜"
            f"费用 {m['fees']:,.0f} 元{extra}")
        self._on_finished()
