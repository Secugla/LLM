# -*- coding: utf-8 -*-
"""
策略选股页面（第三功能模块）
============================
形态选股：按市值/板块/流动性初筛全市场，并发拉取日线，计算日线与周线形态
（均线多头、放量突破、回调企稳、周线金叉、周线多头排列等），输出命中标签、
形态分与涨跌幅，支持排序、增量显示、可中断与导出 CSV。
同时保留原两套严格策略（31 项趋势 / 22 项回调反转）。
"""
import datetime as dt
import os
import threading
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFrame, QComboBox, QSplitter,
                               QTextBrowser, QProgressBar, QFileDialog)

from ui_common import (UP, DOWN, FLAT, WARN, TEXT2, StreamWorker, make_table,
                       cell, pct_color, fmt_amount, skeleton_table,
                       export_table_csv)
import picker_engine

STRATEGY_KEYS = [
    ("pattern", "多形态组合（日线+周线）"),
    ("ma_bull", "日线均线多头"),
    ("breakout", "放量突破"),
    ("pullback", "回调企稳"),
    ("weekly_golden", "周线金叉"),
    ("weekly_bull", "周线多头"),
    ("weekly_breakout", "周线突破"),
    ("trend", "趋势信号（31项严格）"),
    ("reversal", "回调反转（22项严格）"),
    ("all", "严格策略任一命中"),
]
SCOPE_KEYS = [("all", "全部A股"), ("main", "沪深主板"),
              ("gem", "创业板"), ("star", "科创板"), ("bj", "北交所")]
MAX_KEYS = [(200, "200 只（较快）"), (400, "400 只（推荐）"),
            (600, "600 只"), (800, "800 只"), (0, "全部（较慢）")]
# 总市值预设（下限, 上限, 说明, 近一周最低换手峰值），单位亿元
# 「微盘活跃」额外要求近一周存在单日换手 > 8%
MV_KEYS = [(0, 40, "小盘（<40 亿）", 0.0),
           (20, 80, "中小盘（20~80 亿）", 0.0),
           (80, 500, "中盘（80~500 亿）", 0.0),
           (300, 10000, "大盘（>300 亿）", 0.0),
           (0, 10000, "全市场（全部股票）", 0.0),
           (0, 90, "微盘活跃（<90 亿，近一周换手>8%）", 8.0)]
# 最低形态命中：1 宽松（结果多）、2 推荐、3 严格（结果少）
MIN_TAG_KEYS = [(1, "1 个（宽松）"), (2, "2 个（推荐）"), (3, "3 个（严格）")]


class PickerPage(QWidget):
    def __init__(self, set_status: Callable[[str], None]):
        super().__init__()
        self.set_status = set_status
        self._res = None
        self._workers = []
        self._busy = False
        self._stop = threading.Event()
        self._rows = {}
        self._skeleton = True      # 表格是否仍处于骨架占位状态
        self._inc_hits = 0         # 增量命中计数
        self._build()
        skeleton_table(self.tbl, 9, "设置条件后点击“开始选股”")

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        bar = QFrame()
        bar.setObjectName("Card")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(16, 11, 16, 11)
        bl.setSpacing(9)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(QLabel("策略"))
        self.strategy_cb = QComboBox()
        self.strategy_cb.addItems([n for _, n in STRATEGY_KEYS])
        self.strategy_cb.setMinimumWidth(190)
        row1.addWidget(self.strategy_cb)

        row1.addSpacing(10)
        row1.addWidget(QLabel("总市值"))
        self.mv_cb = QComboBox()
        self.mv_cb.addItems([n for _, _, n, _ in MV_KEYS])
        self.mv_cb.setCurrentIndex(4)          # 默认全市场
        self.mv_cb.setMinimumWidth(220)
        self.mv_cb.setToolTip("预设市值区间：选择后自动对应区间下限与上限")
        row1.addWidget(self.mv_cb)

        row1.addSpacing(10)
        row1.addWidget(QLabel("板块"))
        self.scope_cb = QComboBox()
        self.scope_cb.addItems([n for _, n in SCOPE_KEYS])
        row1.addWidget(self.scope_cb)
        row1.addStretch()
        bl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("扫描上限"))
        self.max_cb = QComboBox()
        self.max_cb.addItems([n for _, n in MAX_KEYS])
        self.max_cb.setCurrentIndex(1)
        row2.addWidget(self.max_cb)
        row2.addSpacing(10)
        row2.addWidget(QLabel("最低形态命中"))
        self.min_tags_cb = QComboBox()
        self.min_tags_cb.addItems([n for _, n in MIN_TAG_KEYS])
        self.min_tags_cb.setCurrentIndex(1)
        self.min_tags_cb.setToolTip("仅「多形态组合」策略生效；条件过严时结果为空会自动放宽一档")
        row2.addWidget(self.min_tags_cb)
        row2.addSpacing(10)

        self.btn = QPushButton("开始选股")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.run_scan)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_scan)
        self.export_btn = QPushButton("导出 CSV")
        self.export_btn.clicked.connect(self._export)
        row2.addWidget(self.btn)
        row2.addWidget(self.stop_btn)
        row2.addWidget(self.export_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(150)
        self.progress.setVisible(False)
        row2.addWidget(self.progress)
        row2.addStretch()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        row2.addWidget(self.summary_lbl)
        bl.addLayout(row2)
        root.addWidget(bar)

        self.hint_lbl = QLabel(
            "形态选股：市值/板块/流动性硬过滤 → 日线与周线形态标签 → 形态分排序；"
            "预设区间与命中数为下拉选择，条件过硬时自动放宽一档并标注；"
            "结果不构成投资建议")
        self.hint_lbl.setObjectName("Hint")
        self.hint_lbl.setWordWrap(True)
        root.addWidget(self.hint_lbl)

        # 结果排序工具条：任意时刻（含扫描中）可对已加载结果做顺序/倒序排列
        sort_bar = QHBoxLayout()
        sort_bar.setContentsMargins(0, 0, 0, 0)
        sort_bar.addWidget(QLabel("结果排序："))
        self.sort_field = QComboBox()
        for label, col in [("形态分", 10), ("命中数", 11), ("涨幅%", 5),
                           ("成交额", 9), ("总市值", 6), ("流通市值", 7),
                           ("换手%", 8), ("现价", 4)]:
            self.sort_field.addItem(label, col)
        self.sort_order = QComboBox()
        self.sort_order.addItem("倒序（大 → 小）", Qt.DescendingOrder)
        self.sort_order.addItem("顺序（小 → 大）", Qt.AscendingOrder)
        self.sort_apply = QPushButton("应用排序")
        self.sort_apply.setObjectName("GhostBtn")
        self.sort_apply.setToolTip("对当前表格已加载的结果按所选字段排序；"
                                   "扫描结束后也可以直接点击表头排序")
        self.sort_apply.clicked.connect(self._apply_sort)
        sort_bar.addWidget(self.sort_field)
        sort_bar.addWidget(self.sort_order)
        sort_bar.addWidget(self.sort_apply)
        sort_bar.addStretch()
        root.addLayout(sort_bar)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        self.tbl = make_table(
            ["排名", "代码", "名称", "行业", "现价", "涨幅%", "总市值",
             "流通市值", "换手%", "成交额", "形态分", "命中", "形态标签", "状态"])
        widths = {0: 46, 1: 66, 2: 90, 3: 90, 4: 62, 5: 66, 6: 84,
                  7: 84, 8: 60, 9: 78, 10: 62, 11: 50, 13: 78}
        for i, w in widths.items():
            self.tbl.setColumnWidth(i, w)
        self.tbl.itemSelectionChanged.connect(self._show_detail)
        split.addWidget(self.tbl)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 12)
        self.detail_title = QLabel("形态与条件明细")
        self.detail_title.setObjectName("CardTitle")
        cl.addWidget(self.detail_title)
        self.detail = QTextBrowser()
        self.detail.setHtml(
            f'<div style="color:{TEXT2}; font-size:12px; padding:10px 4px">'
            '点击上方任意个股，查看形态条件命中情况与操作建议</div>')
        cl.addWidget(self.detail)
        split.addWidget(card)
        split.setSizes([430, 300])
        root.addWidget(split, stretch=3)

    # ---------------- 排序 ----------------
    def _apply_sort(self):
        """按所选字段对当前已加载结果做顺序/倒序排列（扫描中也可用）。"""
        if self.tbl.rowCount() == 0:
            self.set_status("还没有可排序的结果，请先开始选股")
            return
        col = int(self.sort_field.currentData())
        order = self.sort_order.currentData()
        self.tbl.setSortingEnabled(True)
        self.tbl.sortItems(col, order)
        # 表头显示排序箭头，之后点击其他表头仍可切换排序
        self.tbl.horizontalHeader().setSortIndicator(col, order)
        label = self.sort_field.currentText()
        arrow = "倒序" if order == Qt.DescendingOrder else "顺序"
        self.set_status(f"已按「{label}」{arrow}排列（{self.tbl.rowCount()} 行）")

    # ---------------- 扫描 ----------------
    def _config(self) -> dict:
        max_n = MAX_KEYS[self.max_cb.currentIndex()][0]
        mv_min, mv_max, _, min_turn = MV_KEYS[self.mv_cb.currentIndex()]
        return {
            "min_mv": mv_min,
            "max_mv": mv_max,
            "require_recent_turnover": min_turn,
            "scope": SCOPE_KEYS[self.scope_cb.currentIndex()][0],
            "max_candidates": max_n,
            "min_tags": MIN_TAG_KEYS[self.min_tags_cb.currentIndex()][0],
        }

    def run_scan(self):
        if self._busy:
            return
        self._busy = True
        self.btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setVisible(True)
        self._stop = threading.Event()
        key = STRATEGY_KEYS[self.strategy_cb.currentIndex()][0]
        cfg = self._config()
        self._res = None
        self._rows = {}
        self._skeleton = True
        self._inc_hits = 0
        self.detail.clear()
        self.summary_lbl.setText("正在准备…")
        skeleton_table(self.tbl, 9, "正在扫描全市场…")
        self.set_status(f"形态选股：{dict(STRATEGY_KEYS)[key]}，正在扫描…")

        def task(p, e):
            return picker_engine.scan(
                key, progress=p, config=cfg, stop_event=self._stop,
                on_results=lambda batch: e("batch", batch))

        w = StreamWorker(task)
        w.progress.connect(self.set_status)
        w.partial.connect(self._on_partial)
        w.done.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_finished)
        self._workers.append(w)
        w.start()

    def _stop_scan(self):
        self._stop.set()
        self.stop_btn.setEnabled(False)
        self.set_status("正在停止…（已完成的候选结果会保留）")

    def _on_finished(self):
        self._busy = False
        self.btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.setVisible(False)
        self._workers = [x for x in self._workers if x.isRunning()]

    def _on_error(self, msg):
        self.set_status("选股失败：" + msg)
        self.summary_lbl.setText("扫描失败")
        self._on_finished()

    def _on_partial(self, stage: str, batch):
        """增量渲染：每批命中先追加到表格，不必等全部扫完。"""
        if stage != "batch" or not batch:
            return
        # 首批结果到达时清空骨架占位行
        if self._skeleton:
            self.tbl.setRowCount(0)
            self._skeleton = False
        self.tbl.setSortingEnabled(False)
        existing = self._inc_hits + len(batch)
        self._inc_hits = existing
        for r in batch:
            self._append_row(r)
        self.summary_lbl.setText(f"扫描中…已命中 {existing} 只")

    def _append_row(self, r: dict):
        row = self.tbl.rowCount()
        self.tbl.insertRow(row)
        tags = "、".join(r.get("tags", []))
        sig = r.get("signal")
        state = "信号" if sig else "观察"
        if r.get("mode") == "pattern":
            state = "形态命中"
        if r.get("relaxed"):
            state = "放宽命中"
        self.tbl.setItem(row, 0, cell(
            r.get("rank", row + 1), UP, bold=True, align=Qt.AlignCenter,
            sort_value=r.get("rank", row + 1)))
        self.tbl.item(row, 0).setData(Qt.UserRole, r)
        self.tbl.setItem(row, 1, cell(r["code"], align=Qt.AlignCenter))
        self.tbl.setItem(row, 2, cell(r["name"], bold=True))
        self.tbl.setItem(row, 3, cell(r.get("industry", ""), TEXT2))
        self.tbl.setItem(row, 4, cell(
            f"{r['price']:.2f}", align=Qt.AlignCenter,
            sort_value=r["price"]))
        self.tbl.setItem(row, 5, cell(
            f"{r['change_pct']:+.2f}", pct_color(r["change_pct"]),
            align=Qt.AlignCenter, sort_value=r["change_pct"]))
        self.tbl.setItem(row, 6, cell(
            f"{r.get('total_mv', 0):.0f}亿", align=Qt.AlignCenter,
            sort_value=r.get("total_mv", 0)))
        self.tbl.setItem(row, 7, cell(
            f"{r.get('circ_mv', 0):.0f}亿", align=Qt.AlignCenter,
            sort_value=r.get("circ_mv", 0)))
        self.tbl.setItem(row, 8, cell(
            f"{r.get('turnover', 0):.1f}", align=Qt.AlignCenter,
            sort_value=r.get("turnover", 0)))
        self.tbl.setItem(row, 9, cell(
            fmt_amount(r.get("amount", 0)), align=Qt.AlignCenter,
            sort_value=r.get("amount", 0)))
        score = r.get("score", 0)
        self.tbl.setItem(row, 10, cell(
            f"{score:.0f}", UP, bold=True, align=Qt.AlignCenter,
            sort_value=score))
        self.tbl.setItem(row, 11, cell(
            r.get("tag_count", 0), align=Qt.AlignCenter,
            sort_value=r.get("tag_count", 0)))
        ctags = cell(tags)
        ctags.setToolTip("；".join(r.get("details", [])[:8]))
        self.tbl.setItem(row, 12, ctags)
        self.tbl.setItem(row, 13, cell(
            f"{state} {r.get('pass_count', 0)}/{r.get('total', 0)}"
            if r.get("mode") == "strict" else state,
            UP if sig else WARN, bold=True, align=Qt.AlignCenter))

    def _on_done(self, res):
        self._res = res
        self._rows = {}
        self._skeleton = False
        self.tbl.setRowCount(0)
        for r in res.get("hits", []):
            self._append_row(r)
        self.tbl.setSortingEnabled(True)
        f = res.get("filter_stats") or {}
        self.summary_lbl.setText(
            f"候选 {res.get('candidates', 0)} 只 · 已扫 {res.get('scanned', 0)}"
            f" · 命中 {len(res.get('hits', []))} 只 · 耗时 {res.get('elapsed', 0)}s"
            + (" · 已放宽条件" if res.get("relaxed") else ""))
        tips = [f"{k}剔除{v}只" for k, v in f.items()]
        if res.get("relaxed_note"):
            tips.append(res["relaxed_note"])
        self.summary_lbl.setToolTip("；".join(tips))
        msg = (f"选股完成：{res.get('strategy_name', '')}，"
               f"命中 {len(res.get('hits', []))} 只，耗时 {res.get('elapsed', 0)}s")
        if res.get("relaxed"):
            msg += "（原条件无命中，已自动放宽展示）"
        if res.get("cancelled"):
            msg += "（已中断）"
        self.set_status(msg)
        if res.get("hits"):
            self.tbl.selectRow(0)
        else:
            self.detail.setHtml(
                f"<span style='color:{WARN}'>本次条件下没有命中标的。"
                f"建议：放宽市值区间 / 调低「最低形态命中」/ 增大扫描上限。</span>")
        self._on_finished()

    # ---------------- 明细 ----------------
    def _show_detail(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return
        item = self.tbl.item(rows[0].row(), 0)
        r = item.data(Qt.UserRole) if item else None
        if not r:
            return
        html = f"<b style='font-size:14px;color:{UP}'>"
        html += f"#{r.get('rank', '')} {r['code']} {r['name']}</b> "
        html += f"现价 {r['price']:.2f} · 涨 {r['change_pct']:+.2f}% · "
        html += f"总市值 {r.get('total_mv', 0):.0f}亿 · "
        html += f"换手 {r.get('turnover', 0):.1f}% · "
        html += f"形态分 <b style='color:{UP}'>{r.get('score', 0):.0f}</b>"
        if r.get("mode") == "pattern":
            html += f" · 命中 {r.get('tag_count', 0)} 个形态"
        else:
            html += (f" · 通过 {r.get('pass_count', 0)}/{r.get('total', 0)}"
                     f"（{'信号' if r.get('signal') else '观察'}）")
        html += "<br><br>"

        daily = r.get("daily_tags") or []
        weekly = r.get("weekly_tags") or []
        if daily or weekly:
            html += f"<b>日线形态：</b>{'、'.join(daily) if daily else '无'}<br>"
            html += f"<b>周线形态：</b>{'、'.join(weekly) if weekly else '无'}<br><br>"

        ex_notes = r.get("extra_notes") or []
        if ex_notes:
            html += (f"<b>额外筛选：</b>"
                     f"<span style='color:{WARN}'>{'；'.join(ex_notes)}</span>"
                     f"<br><br>")

        if r.get("details"):
            html += f"<b>形态命中依据：</b><br>"
            for d in r["details"]:
                html += f"<span style='color:{DOWN};font-weight:bold'>✓</span> {d}<br>"
            html += "<br>"

        passed = r.get("passed") or []
        failed = r.get("failed") or []
        if r.get("mode") == "strict":
            html += "<b>条件逐条：</b><br>"
            for x in passed:
                html += (f"<span style='color:{DOWN};font-weight:bold'>✓</span> "
                         f"{x.get('no', '')} {x.get('name', '')}"
                         f"<span style='color:{TEXT2}'>（当前 {x.get('cur', '')}）"
                         f"</span><br>")
            for x in failed:
                html += (f"<span style='color:{UP};font-weight:bold'>✗</span> "
                         f"{x.get('no', '')} {x.get('name', '')}"
                         f"<span style='color:{TEXT2}'>（当前 {x.get('cur', '')}，"
                         f"要求 {x.get('req', '')}）</span><br>")
            if self._res and self._res.get("risk"):
                html += ("<hr style='border:none;border-top:1px solid #E6E8EB'>"
                         "<b>持仓风控（出信号后执行）：</b><br>")
                for no, name, desc in self._res["risk"]:
                    html += f"{no} {name}：{desc}<br>"
        if self._res and r.get("relaxed") and self._res.get("relaxed_note"):
            html += (f"<hr style='border:none;border-top:1px solid #E6E8EB'>"
                     f"<span style='color:{WARN}'>"
                     f"{self._res['relaxed_note']}</span>")
        self.detail.setHtml(html)

    # ---------------- 导出 ----------------
    def _export(self):
        if not self._res or not self._res.get("hits"):
            self.set_status("暂无可导出的选股结果")
            return
        key = STRATEGY_KEYS[self.strategy_cb.currentIndex()][0]
        default = os.path.join(
            os.path.expanduser("~"), "Desktop",
            f"形态选股_{key}_{dt.date.today():%Y%m%d}.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出选股结果", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            n = export_table_csv(self.tbl, path)
            self.set_status(f"已导出 {n} 条到 {path}")
        except Exception as e:  # noqa
            self.set_status(f"导出失败：{e}")
