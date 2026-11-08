# -*- coding: utf-8 -*-
"""量化研究页：因子有效性、参数稳健性、机器学习与历史记录。"""
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QLineEdit, QProgressBar, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from ui_common import (FLAT, TEXT, TEXT2, WARN, Worker, cell, make_table,
                       pct_color, skeleton_table)
import market_info
import quant_lab
import strategy_library as SL


class QuantLabPage(QWidget):
    def __init__(self, set_status):
        super().__init__()
        self.set_status = set_status
        self._workers = []
        self._cancel = threading.Event()
        self._busy = False
        self._build()

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        ctrl.addWidget(QLabel("策略"))
        self.strategy = QComboBox()
        self.strategy.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.strategy.setMinimumContentsLength(10)
        for s in SL.list_strategies():
            self.strategy.addItem(f"{s['name']} · {s['org']}", s["id"])
        ctrl.addWidget(self.strategy)

        ctrl.addWidget(QLabel("回测长度"))
        self.months = QComboBox()
        for n in (3, 6, 12, 18, 24, 36, 60):
            self.months.addItem(f"{n} 个月", n)
        self.months.setCurrentIndex(self.months.findData(18))
        self.months.setFixedWidth(100)
        ctrl.addWidget(self.months)

        ctrl.addWidget(QLabel("股票池"))
        self.pool = QComboBox()
        for n in (50, 100, 200, 400, 800):
            self.pool.addItem(f"{n} 只", n)
        self.pool.setCurrentIndex(self.pool.findData(200))
        self.pool.setFixedWidth(90)
        ctrl.addWidget(self.pool)

        ctrl.addWidget(QLabel("预测周期"))
        self.horizon = QComboBox()
        for n in (5, 10, 20, 30, 60):
            self.horizon.addItem(f"{n} 日", n)
        self.horizon.setCurrentIndex(self.horizon.findData(20))
        self.horizon.setFixedWidth(86)
        ctrl.addWidget(self.horizon)

        self.neutral = QCheckBox("行业/市值中性化")
        self.neutral.setChecked(True)
        ctrl.addWidget(self.neutral)
        self.fund = QCheckBox("资金面（龙虎榜+北向）")
        self.fund.setChecked(True)
        ctrl.addWidget(self.fund)
        root.addLayout(ctrl)

        ctrl2 = QHBoxLayout()
        ctrl2.setSpacing(10)
        self.factor_btn = QPushButton("① 因子有效性检验")
        self.factor_btn.clicked.connect(self.run_factor)
        self.grid_btn = QPushButton("② 参数稳健性网格")
        self.grid_btn.clicked.connect(self.run_grid)
        self.ml_btn = QPushButton("③ 机器学习滚动训练")
        self.ml_btn.clicked.connect(self.run_ml)
        self.seat_btn = QPushButton("④ 席位/游资画像")
        self.seat_btn.setObjectName("GhostBtn")
        self.seat_btn.clicked.connect(self.run_seat)
        ctrl2.addWidget(self.factor_btn)
        ctrl2.addWidget(self.grid_btn)
        ctrl2.addWidget(self.ml_btn)
        ctrl2.addWidget(self.seat_btn)
        ctrl2.addWidget(QLabel("网格标的"))
        self.code = QLineEdit("601566")
        self.code.setFixedWidth(88)
        self.code.setPlaceholderText("6 位代码")
        ctrl2.addWidget(self.code)
        ctrl2.addStretch()
        self.refresh_btn = QPushButton("刷新历史")
        self.refresh_btn.setObjectName("GhostBtn")
        self.refresh_btn.clicked.connect(self.load_history)
        ctrl2.addWidget(self.refresh_btn)
        root.addLayout(ctrl2)

        self.summary = QLabel("选择参数后点击上方按钮运行；数据来自本地免费行情源。")
        self.summary.setStyleSheet(f"color:{TEXT2};")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        src_lbl = QLabel(
            "数据源：东财快照（含 PE/PB、60 日/年内涨跌）· 新浪/腾讯/东财日线 · "
            "板块轮动 · F10 题材 · 新闻情绪 · 龙虎榜 · 龙虎榜席位/游资画像 · "
            "北向成交额与十大活跃股｜"
            "暂未接入：财报 ROE/营收利润、宏观指标、券商交易接口")
        src_lbl.setObjectName("Hint")
        src_lbl.setWordWrap(True)
        root.addWidget(src_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setVisible(False)
        root.addWidget(self.bar)

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(380)

        self.factor_tbl = make_table(
            ["排名", "因子", "权重", "IC均值", "ICIR", "RankIC",
             "RankICIR", "IC为正比例"])
        for i, w in enumerate([52, 94, 58, 82, 66, 82, 84, 96]):
            self.factor_tbl.setColumnWidth(i, w)
        self.layer_tbl = make_table(
            ["分层", "说明", "20日平均收益", "年化收益", "胜率", "样本数"])
        for i, w in enumerate([60, 92, 120, 110, 74, 76]):
            self.layer_tbl.setColumnWidth(i, w)
        self.grid_tbl = make_table(
            ["入场阈值", "止损%", "全样本收益", "全样本超额", "最大回撤",
             "夏普", "胜率", "样本外收益", "样本外超额", "判定"])
        for i, w in enumerate([84, 66, 96, 96, 88, 62, 66, 94, 94, 96]):
            self.grid_tbl.setColumnWidth(i, w)
        self.ml_tbl = make_table(["排名", "因子", "回归权重"])
        for i, w in enumerate([60, 120, 100]):
            self.ml_tbl.setColumnWidth(i, w)
        self.hist_tbl = make_table(
            ["时间", "模式", "标的", "策略", "模型版本", "总收益", "超额", "回撤",
             "夏普", "胜率", "中性化", "执行约束", "资金面"])
        for i, w in enumerate([136, 92, 100, 150, 86, 82, 78, 78, 62, 66,
                               66, 76, 62]):
            self.hist_tbl.setColumnWidth(i, w)
        self.seat_tbl = make_table(
            ["席位 / 别名", "近5日上榜", "1日涨幅", "3日涨幅", "5日涨幅",
             "10日涨幅", "5日胜率"])
        for i, w in enumerate([300, 92, 82, 82, 82, 86, 82]):
            self.seat_tbl.setColumnWidth(i, w)
        self.seat_tbl.setSortingEnabled(True)
        self.tabs.addTab(self.factor_tbl, "因子有效性")
        self.tabs.addTab(self.layer_tbl, "分层收益")
        self.tabs.addTab(self.grid_tbl, "参数网格")
        self.tabs.addTab(self.ml_tbl, "机器学习")
        self.tabs.addTab(self.hist_tbl, "历史记录")
        self.tabs.addTab(self.seat_tbl, "游资席位")
        root.addWidget(self.tabs, stretch=1)

        skeleton_table(self.factor_tbl, 5, "点击“因子有效性检验”后展示 IC/RankIC")
        skeleton_table(self.layer_tbl, 5, "分层收益将在此展示")
        skeleton_table(self.grid_tbl, 5, "点击“参数稳健性网格”后展示")
        skeleton_table(self.ml_tbl, 5, "点击“机器学习滚动训练”后展示")
        skeleton_table(self.seat_tbl, 6, "点击“④ 席位/游资画像”拉取营业部上榜后的表现")
        self.load_history()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self.hist_tbl.rowCount() <= 5:
            self.load_history()

    # ---------------- 运行控制 ----------------
    def _set_busy(self, busy: bool, msg: str = ""):
        self._busy = busy
        for b in (self.factor_btn, self.grid_btn, self.ml_btn, self.seat_btn):
            b.setEnabled(not busy)
        self.bar.setVisible(busy)
        if not busy:
            self.bar.setValue(0)
        if msg:
            self.set_status(msg)

    def _start(self, fn, on_done, label: str):
        if self._busy:
            self.set_status("已有任务在运行，请稍候")
            return
        self._cancel.clear()
        self._set_busy(True, f"正在运行：{label}…")
        self.bar.setValue(0)
        w = Worker(fn)
        w.progress.connect(lambda s: (self.set_status(s),
                                      self.bar.setValue(min(95, self.bar.value() + 2))))
        w.done.connect(on_done)
        w.error.connect(self._on_error)
        w.finished.connect(lambda: self._set_busy(False, f"{label}完成"))
        self._workers.append(w)
        w.start()

    def _on_error(self, msg):
        self._set_busy(False, f"运行失败：{msg}")
        skeleton_table(self.factor_tbl, 5, f"运行失败：{msg[:40]}")

    def _params(self):
        return {
            "months": int(self.months.currentData()),
            "pool_size": int(self.pool.currentData()),
            "horizon": int(self.horizon.currentData()),
            "neutralize": self.neutral.isChecked(),
            "fund_boost": self.fund.isChecked(),
            "strategy_id": self.strategy.currentData(),
        }

    # ---------------- 因子检验 ----------------
    def run_factor(self):
        self.skeleton("factor", "正在计算因子 IC…")
        self._start(
            lambda p: quant_lab.factor_analysis(progress=p,
                                                cancel=self._cancel,
                                                **self._params()),
            self._fill_factor, "因子有效性检验")

    def _fill_factor(self, r):
        self.tabs.setCurrentIndex(0)
        ft = self.factor_tbl
        ft.setSortingEnabled(False)
        ft.setRowCount(0)
        for i, f in enumerate(r["factors"], 1):
            rr = ft.rowCount()
            ft.insertRow(rr)
            ft.setItem(rr, 0, cell(str(i), TEXT2, align=Qt.AlignCenter))
            ft.setItem(rr, 1, cell(f["name"], TEXT, bold=True))
            ft.setItem(rr, 2, cell(f"{f['weight']:.0f}",
                                   align=Qt.AlignCenter))
            ft.setItem(rr, 3, cell(self._num(f["ic"], 4),
                                   pct_color((f["ic"] or 0) * 10),
                                   align=Qt.AlignCenter))
            ft.setItem(rr, 4, cell(self._num(f["ic_ir"]),
                                   align=Qt.AlignCenter))
            ft.setItem(rr, 5, cell(self._num(f["rank_ic"], 4),
                                   pct_color((f["rank_ic"] or 0) * 10),
                                   align=Qt.AlignCenter))
            ft.setItem(rr, 6, cell(self._num(f["rank_ic_ir"]),
                                   align=Qt.AlignCenter))
            ft.setItem(rr, 7, cell(self._num(f["rank_ic_positive"], 1, "%"),
                                   align=Qt.AlignCenter))
        ft.setSortingEnabled(True)
        self.tabs.setTabText(0, f"因子有效性（{len(r['factors'])}）")

        lt = self.layer_tbl
        lt.setSortingEnabled(False)
        lt.setRowCount(0)
        for row in r["layers"]:
            rr = lt.rowCount()
            lt.insertRow(rr)
            lt.setItem(rr, 0, cell(row["layer"], TEXT, bold=True,
                                   align=Qt.AlignCenter))
            lt.setItem(rr, 1, cell(row["label"], TEXT2, align=Qt.AlignCenter))
            lt.setItem(rr, 2, cell(f"{row['avg_return']:+.2f}%",
                                   pct_color(row["avg_return"]),
                                   align=Qt.AlignCenter))
            lt.setItem(rr, 3, cell(f"{row['annual_return']:+.2f}%",
                                   pct_color(row["annual_return"]),
                                   align=Qt.AlignCenter))
            lt.setItem(rr, 4, cell(f"{row['win_rate']:.1f}%",
                                   align=Qt.AlignCenter))
            lt.setItem(rr, 5, cell(str(row["samples"]), TEXT2,
                                   align=Qt.AlignCenter))
        lt.setSortingEnabled(True)
        self.summary.setText(
            f"区间 {r['period']}｜横截面 {r['samples']} 个｜预测周期 "
            f"{r['horizon']} 日｜策略 {r['strategy']}｜"
            f"最高分-最低分年化差 {r['top_bottom_spread']:+.2f}%"
            f"｜中性化 {'开' if r['neutralize'] else '关'}"
            f"｜资金面 {'开' if r.get('fund_boost') else '关'}")

    # ---------------- 席位/游资画像 ----------------
    def run_seat(self):
        self.skeleton("seat", "正在拉取营业部上榜后的表现…")
        self._start(lambda p: market_info.fetch_seat_rank(200),
                    self._fill_seat, "席位/游资画像")

    def _fill_seat(self, rows):
        self.tabs.setCurrentIndex(5)
        t = self.seat_tbl
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for x in rows:
            rr = t.rowCount()
            t.insertRow(rr)
            alias = x.get("alias") or ""
            txt = f"{alias}｜{x['seat']}" if alias else x["seat"]
            c0 = cell(txt, TEXT, bold=bool(alias))
            c0.setToolTip(x["seat"])
            t.setItem(rr, 0, c0)
            t.setItem(rr, 1, cell(str(x["n5"]), TEXT2, align=Qt.AlignCenter,
                                  sort_value=x["n5"]))
            for i, key in enumerate(("avg1", "avg3", "avg5", "avg10"), start=2):
                v = float(x.get(key) or 0.0)
                t.setItem(rr, i, cell(f"{v:+.2f}%", pct_color(v),
                                      align=Qt.AlignCenter, sort_value=v))
            p5 = float(x.get("prob5") or 0.0)
            t.setItem(rr, 6, cell(f"{p5:.1f}%", pct_color(p5 - 50),
                                  align=Qt.AlignCenter, sort_value=p5))
        t.setSortingEnabled(True)
        self.tabs.setTabText(5, f"游资席位（{len(rows)}）")
        strong = sum(1 for x in rows if float(x.get("avg5") or 0) > 0)
        self.summary.setText(
            f"营业部 {len(rows)} 家｜上榜后 5 日平均为正的有 {strong} 家｜"
            f"涨幅与胜率来自东财营业部统计（历史，不代表未来）｜"
            f"别名映射见 .cache/seat_alias.json")

    # ---------------- 参数网格 ----------------
    def run_grid(self):
        code = (self.code.text() or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            self.set_status("请输入 6 位股票代码")
            return
        self.skeleton("grid", "正在逐组回测…")
        self._start(
            lambda p: quant_lab.param_grid(
                code, int(self.months.currentData()), 200000,
                self.strategy.currentData(),
                fund_boost=self.fund.isChecked(),
                progress=p, cancel=self._cancel),
            self._fill_grid, f"参数网格 {code}")

    def _fill_grid(self, r):
        self.tabs.setCurrentIndex(2)
        gt = self.grid_tbl
        gt.setSortingEnabled(False)
        gt.setRowCount(0)
        best = r["best"]
        for row in r["rows"]:
            f, o = row["full"], row["oos"]
            rr = gt.rowCount()
            gt.insertRow(rr)
            gt.setItem(rr, 0, cell(str(row["entry"]), align=Qt.AlignCenter,
                                   sort_value=row["entry"]))
            gt.setItem(rr, 1, cell(str(row["trail"]), align=Qt.AlignCenter,
                                   sort_value=row["trail"]))
            gt.setItem(rr, 2, cell(self._pct(f.get("total_return")),
                                   pct_color(f.get("total_return")),
                                   align=Qt.AlignCenter))
            gt.setItem(rr, 3, cell(self._pct(f.get("excess_return")),
                                   pct_color(f.get("excess_return")),
                                   align=Qt.AlignCenter))
            gt.setItem(rr, 4, cell(self._pct(f.get("max_drawdown")),
                                   align=Qt.AlignCenter))
            gt.setItem(rr, 5, cell(self._num(f.get("sharpe")),
                                   align=Qt.AlignCenter))
            gt.setItem(rr, 6, cell(self._num(f.get("win_rate"), 1, "%"),
                                   align=Qt.AlignCenter))
            gt.setItem(rr, 7, cell(self._pct(o.get("total_return")),
                                   pct_color(o.get("total_return")),
                                   align=Qt.AlignCenter))
            gt.setItem(rr, 8, cell(self._pct(o.get("excess_return")),
                                   pct_color(o.get("excess_return")),
                                   align=Qt.AlignCenter))
            tag = "优选" if (row["entry"] == best["entry"]
                            and row["trail"] == best["trail"]) else ""
            gt.setItem(rr, 9, cell(tag, WARN if tag else TEXT2,
                                   bold=bool(tag), align=Qt.AlignCenter))
        gt.setSortingEnabled(True)
        spread = ("·存在过拟合风险" if r["overfit"] else "·样本外未明显衰减")
        self.summary.setText(
            f"{r['code']} {r['name']}｜全样本 {r['months']} 个月，样本外 "
            f"{r['oos_months']} 个月｜最优组合 入场 {best['entry']} / "
            f"止损 {best['trail']}%｜全样本超额 "
            f"{self._pct(best['full'].get('excess_return'))}，样本外 "
            f"{self._pct(best['oos'].get('excess_return'))}"
            f"{spread}（{len(r['rows'])} 组）")

    # ---------------- 机器学习 ----------------
    def run_ml(self):
        self.skeleton("ml", "正在滚动训练…")
        self._start(
            lambda p: quant_lab.ml_walkforward(progress=p,
                                               cancel=self._cancel,
                                               **self._params()),
            self._fill_ml, "机器学习滚动训练")

    def _fill_ml(self, r):
        self.tabs.setCurrentIndex(3)
        mt = self.ml_tbl
        mt.setSortingEnabled(False)
        mt.setRowCount(0)
        for i, row in enumerate(r["importance"], 1):
            rr = mt.rowCount()
            mt.insertRow(rr)
            mt.setItem(rr, 0, cell(str(i), TEXT2, align=Qt.AlignCenter))
            mt.setItem(rr, 1, cell(row["name"], TEXT, bold=True))
            v = row["weight"]
            mt.setItem(rr, 2, cell(f"{v:+.2f}", pct_color(v * 10),
                                   align=Qt.AlignCenter, sort_value=v))
        mt.setSortingEnabled(True)
        self.summary.setText(
            f"滚动窗口 {r['windows']} 个｜样本外 RankIC "
            f"{self._num(r['rank_ic'], 4)}（人工权重基线 "
            f"{self._num(r['baseline_rank_ic'], 4)}）｜ICIR "
            f"{self._num(r['ic_ir'])}｜IC 为正比例 "
            f"{self._num(r['positive'], 1, '%')}｜Top20% 平均超额 "
            f"{self._num(r['top_excess'], 2, '%')}")

    # ---------------- 历史记录 ----------------
    def load_history(self):
        rows = quant_lab.load_history(40)
        t = self.hist_tbl
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for rec in rows:
            rr = t.rowCount()
            t.insertRow(rr)
            m = rec.get("metrics") or {}
            t.setItem(rr, 0, cell(rec.get("time", ""), TEXT2,
                                  align=Qt.AlignCenter))
            t.setItem(rr, 1, cell(rec.get("mode_name", ""),
                                  align=Qt.AlignCenter))
            t.setItem(rr, 2, cell(rec.get("title", "")))
            p = rec.get("params") or {}
            strat = p.get("strategy_name") or p.get("strategy_id") or ""
            t.setItem(rr, 3, cell(str(strat)))
            t.setItem(rr, 4, cell(p.get("model_version") or "—", TEXT2,
                                  align=Qt.AlignCenter))
            t.setItem(rr, 5, cell(self._pct(m.get("total_return")),
                                  pct_color(m.get("total_return")),
                                  align=Qt.AlignCenter))
            t.setItem(rr, 6, cell(self._pct(m.get("excess_return")),
                                  pct_color(m.get("excess_return")),
                                  align=Qt.AlignCenter))
            t.setItem(rr, 7, cell(self._pct(m.get("max_drawdown")),
                                  align=Qt.AlignCenter))
            t.setItem(rr, 8, cell(self._num(m.get("sharpe")),
                                  align=Qt.AlignCenter))
            t.setItem(rr, 9, cell(self._num(m.get("win_rate"), 1, "%"),
                                  align=Qt.AlignCenter))
            fl = rec.get("flags") or {}
            t.setItem(rr, 10, cell("开" if fl.get("neutralize") else "关",
                                   align=Qt.AlignCenter))
            t.setItem(rr, 11, cell("开" if fl.get("exec_constraints") else "关",
                                   align=Qt.AlignCenter))
            t.setItem(rr, 12, cell("开" if fl.get("fund_boost") else "关",
                                   align=Qt.AlignCenter))
        t.setSortingEnabled(True)
        self.tabs.setTabText(4, f"历史记录（{len(rows)}）")

    # ---------------- 小工具 ----------------
    def skeleton(self, which: str, msg: str):
        table = {"factor": self.factor_tbl, "grid": self.grid_tbl,
                 "ml": self.ml_tbl, "seat": self.seat_tbl}.get(which)
        if table is not None:
            skeleton_table(table, 5, msg)

    @staticmethod
    def _num(v, digits=2, suffix=""):
        if v is None:
            return "—"
        return f"{v:.{digits}f}{suffix}"

    @staticmethod
    def _pct(v):
        return "—" if v is None else f"{v:+.2f}%"
