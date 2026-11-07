# -*- coding: utf-8 -*-
"""量化回测页面：参数、净值/回撤/仓位图、绩效指标、每日操作明细。"""
import datetime as dt
from typing import Callable

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFrame, QLineEdit, QSplitter,
                               QProgressBar)
import pyqtgraph as pg

from ui_common import (UP, DOWN, FLAT, CARD, TEXT, TEXT2, BORDER,
                       Worker, make_table, cell, pct_color, skeleton_table)
import backtest_service

pg.setConfigOption("background", CARD)
pg.setConfigOption("foreground", TEXT2)


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
                d = self.dates[i]
                out.append(d[5:])      # MM-DD
            else:
                out.append("")
        return out


class MetricCard(QFrame):
    def __init__(self, label):
        super().__init__()
        self.setObjectName("Card")
        self.setMinimumWidth(100)
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
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # 参数栏
        bar = QFrame()
        bar.setObjectName("Card")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 11, 16, 11)
        bl.setSpacing(8)
        today = dt.date.today()
        self.code = QLineEdit("601566")
        self.start = QLineEdit((today - dt.timedelta(days=730)).strftime("%Y%m%d"))
        self.end = QLineEdit(today.strftime("%Y%m%d"))
        self.fast = QLineEdit("5")
        self.slow = QLineEdit("20")
        self.cash = QLineEdit("50000")
        self.btn = QPushButton("运行回测")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.run)
        for lab, w in [("代码", self.code), ("开始", self.start),
                       ("结束", self.end), ("快均线", self.fast),
                       ("慢均线", self.slow), ("初始资金", self.cash)]:
            bl.addWidget(QLabel(lab + "："))
            w.setMaximumWidth(96)
            bl.addWidget(w)
        bl.addStretch()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(140)
        self.progress.setVisible(False)
        bl.addWidget(self.progress)
        bl.addWidget(self.btn)
        root.addWidget(bar)

        # 指标卡片
        self.metrics = {}
        mrow = QHBoxLayout()
        for key, lab in [("total_return", "总收益%"), ("annual_return", "年化%"),
                         ("excess_return", "超额%"), ("max_drawdown", "最大回撤%"),
                         ("sharpe", "夏普"), ("win_rate", "胜率%"),
                         ("trades", "交易笔数")]:
            c = MetricCard(lab)
            self.metrics[key] = c
            mrow.addWidget(c)
        root.addLayout(mrow)

        # 图表
        self.gw = pg.GraphicsLayoutWidget()
        self.gw.setFrameShape(QFrame.StyledPanel)
        self.gw.setStyleSheet(f"border:1px solid {BORDER}; border-radius:6px;")
        root.addWidget(self.gw, stretch=3)

        # 每日操作
        split = QSplitter(Qt.Vertical)
        self.daily_tbl = make_table(
            ["日期", "操作", "价格", "数量(股)", "仓位%", "现金", "总资产", "说明"])
        self.daily_tbl.setColumnWidth(0, 90)
        self.daily_tbl.setColumnWidth(1, 60)
        self.daily_tbl.setColumnWidth(7, 320)
        split.addWidget(self.daily_tbl)
        root.addWidget(split, stretch=2)
        skeleton_table(self.daily_tbl, 6, "点击“运行回测”后显示每日操作")

    def run(self):
        try:
            params = dict(
                code=self.code.text().strip(),
                start=self.start.text().strip(),
                end=self.end.text().strip(),
                fast=int(self.fast.text()),
                slow=int(self.slow.text()),
                init_cash=float(self.cash.text()))
        except Exception:
            self.set_status("参数填写有误")
            return
        if params["fast"] >= params["slow"]:
            self.set_status("快均线周期必须小于慢均线")
            return
        self.btn.setEnabled(False)
        self.progress.setVisible(True)
        self.set_status("正在运行回测…")
        w = Worker(lambda p: backtest_service.run_backtest(
            **params, progress=p))
        w.progress.connect(self.set_status)
        w.done.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_finished)
        self._workers.append(w)
        w.start()

    def _on_error(self, msg):
        self.set_status("回测失败：" + msg)
        self._on_finished()

    def _on_finished(self):
        self.btn.setEnabled(True)
        self.progress.setVisible(False)
        self._workers = [x for x in self._workers if x.isRunning()]

    def _on_done(self, r):
        m = r["metrics"]
        curve = r.get("curve") or []
        if not curve:
            self.set_status("回测完成，但区间内没有可用行情数据")
            skeleton_table(self.daily_tbl, 6, "区间内没有可用行情数据")
            self._on_finished()
            return
        # 指标
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
        self.metrics["trades"].set_val(f"{m['trades']}")

        # 图表
        dates = [c["date"] for c in r["curve"]]
        x = np.arange(len(dates))
        nav = [c["nav"] / r["init_cash"] for c in r["curve"]]
        bench = [c["bench"] / r["init_cash"] for c in r["curve"]]
        dd = [(c["nav"] / max(r["curve"][:i + 1], key=lambda z: z["nav"])["nav"] - 1) * 100
              for i, c in enumerate(r["curve"])]
        pos = [c["position_pct"] for c in r["curve"]]

        self.gw.clear()
        ax = DateAxis(dates)
        p1 = self.gw.addPlot(row=0, col=0, axisItems={"bottom": ax})
        p1.plot(x, nav, pen=pg.mkPen(UP, width=2), name="策略净值")
        p1.plot(x, bench, pen=pg.mkPen(FLAT, width=1.4, style=Qt.DashLine),
                name="买入持有")
        p1.showGrid(x=True, y=True, alpha=0.25)
        p1.setLabel("left", "净值（归一化）")
        p1.addLegend(offset=(-10, 10))
        p1.setXLink(None)

        p2 = self.gw.addPlot(row=1, col=0)
        p2.plot(x, dd, pen=pg.mkPen(DOWN, width=1),
                fillLevel=0, fillBrush=pg.mkBrush(224, 51, 46, 60))
        p2.showGrid(x=True, y=True, alpha=0.25)
        p2.setLabel("left", "回撤%")

        p3 = self.gw.addPlot(row=2, col=0)
        p3.plot(x, pos, pen=pg.mkPen("#2E6FD6", width=1.4),
                fillLevel=0, fillBrush=pg.mkBrush(46, 111, 214, 40))
        p3.showGrid(x=True, y=True, alpha=0.25)
        p3.setLabel("left", "仓位%")
        p3.setYRange(0, 105)
        for p in (p1, p2):
            p.getAxis("bottom").setStyle(showValues=False)
        p2.setXLink(p3)
        p1.setXLink(p3)

        # 每日操作
        self.daily_tbl.setSortingEnabled(False)
        self.daily_tbl.setRowCount(0)
        for d in r["daily"]:
            rr = self.daily_tbl.rowCount()
            self.daily_tbl.insertRow(rr)
            self.daily_tbl.setItem(rr, 0, cell(d["date"], TEXT2,
                                               align=Qt.AlignCenter))
            ac = d["action"]
            ac_c = UP if ac == "买入" else (DOWN if ac == "卖出" else FLAT)
            self.daily_tbl.setItem(rr, 1, cell(ac, ac_c, bold=True,
                                               align=Qt.AlignCenter))
            self.daily_tbl.setItem(rr, 2, cell(d["price"],
                                               align=Qt.AlignCenter))
            self.daily_tbl.setItem(rr, 3, cell(f"{d['shares']:,}",
                                               align=Qt.AlignCenter))
            self.daily_tbl.setItem(rr, 4, cell(f"{d['position_pct']:.1f}",
                                               "#2E6FD6",
                                               align=Qt.AlignCenter))
            self.daily_tbl.setItem(rr, 5, cell(f"{d['cash']:,.0f}",
                                               align=Qt.AlignCenter))
            self.daily_tbl.setItem(rr, 6, cell(f"{d['total']:,.0f}",
                                               bold=True, align=Qt.AlignCenter))
            self.daily_tbl.setItem(rr, 7, cell(d["note"]))

        self.daily_tbl.setSortingEnabled(True)
        self.set_status(
            f"回测完成：{r['code']} {r['start']}~{r['end']} "
            f"总收益{m['total_return']}% 超额{m['excess_return']}%")
