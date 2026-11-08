# -*- coding: utf-8 -*-
"""
策略学习页（第六功能模块）
==========================
左侧：机构策略库列表（幻方/九坤/明汯/灵均/WorldQuant/经典多因子）。
右侧上：策略说明（一句话 / 公开事实 / 媒体推断（未证实） / 映射到本系统
        因子 / 数据要求 / 风险与局限 / 参考来源）。
右侧中：套用该策略生成的真实选股结果（可排序、可导出）。
右侧下：选中个股的因子构成与理由。

重要：本页只做「公开信息学习 + 近似实现」。机构真实因子公式、权重与持仓
从未公开，结果不代表任何机构策略，也不构成投资建议。
"""

import datetime as dt
import os
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem,
                               QProgressBar, QPushButton, QSplitter,
                               QTextBrowser, QVBoxLayout, QWidget)

import market_info
import recommender
import strategy_library as lib
from ui_common import (TEXT2, UP, WARN, Worker, cell, export_table_csv,
                       make_table, skeleton_table)

FACTOR_KEYS = lib.FACTOR_KEYS
FACTOR_LABEL = {
    "trend": "趋势", "volume": "量价", "liquidity": "流动",
    "rotation": "轮动", "leader": "龙头", "theme": "题材",
    "capital": "资金", "risk": "风险",
}


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_strategy_html(s: dict) -> str:
    """把策略字典渲染为说明 HTML。"""
    html = (f"<div style='font-size:16px; font-weight:bold; color:{UP}'>"
            f"{_esc(s['name'])}</div>"
            f"<div style='color:{TEXT2}; margin:2px 0 8px 0'>"
            f"{_esc(s['org'])} · {_esc(s['tag'])}</div>")
    html += f"<p><b>一句话：</b>{_esc(s['one_liner'])}</p>"

    def section(title: str, items, color: str = ""):
        nonlocal html
        if not items:
            return
        html += f"<div style='margin-top:8px'><b>{title}</b></div>"
        html += "<ul style='margin:4px 0 0 18px'>"
        for it in items:
            prefix = f"<span style='color:{color}'>" if color else ""
            suffix = "</span>" if color else ""
            html += f"<li>{prefix}{_esc(it)}{suffix}</li>"
        html += "</ul>"

    section("公开事实（可核实）", s.get("public_facts", []))
    section("媒体推断 / 未证实（仅作学习参考）", s.get("inferred", []),
            color=WARN)

    mapping = s.get("mapping") or {}
    if mapping:
        html += "<div style='margin-top:8px'><b>映射到本系统 8 因子</b></div>"
        html += "<table cellspacing='0' cellpadding='3' width='100%'>"
        for key in FACTOR_KEYS:
            w = (s.get("weights") or {}).get(key, 0)
            html += (f"<tr><td width='110'><b>{FACTOR_NAME(key)}</b></td>"
                     f"<td width='60' align='right'>权重 {w:.0f}%</td>"
                     f"<td>{_esc(mapping.get(key, '—'))}</td></tr>")
        html += "</table>"

    section("数据要求", s.get("data_needs", []))
    section("风险与局限", s.get("risks", []), color=WARN)

    links = lib.source_links(s)
    if links:
        html += "<div style='margin-top:8px'><b>参考来源</b></div><ul style='margin:4px 0 0 18px'>"
        for title, url in links:
            html += (f"<li><a href='{_esc(url)}'>{_esc(title)}</a></li>")
        html += "</ul>"

    html += (f"<p style='color:{WARN}; margin-top:10px'>"
             "免责声明：公开资料不等于机构真实策略。本页只是把公开方法论"
             "映射到本系统的 8 个可计算因子做近似选股，不构成投资建议。</p>")
    return html


def FACTOR_NAME(key: str) -> str:
    return lib.FACTOR_NAME.get(key, key)


class StrategyPage(QWidget):
    def __init__(self, set_status: Callable[[str], None],
                 snapshot_provider: Callable[[], Optional[object]]):
        super().__init__()
        self.set_status = set_status
        self.provider = snapshot_provider
        self._workers = []
        self._busy = False
        self._results = {}       # sid -> 荐股结果
        self._meta = {}          # 当前结果的生效因子满分
        self._rows = {}
        self._build()
        self.list.setCurrentRow(0)

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # 顶部工具栏
        bar = QFrame()
        bar.setObjectName("Card")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 10)
        bl.setSpacing(10)
        title = QLabel("机构策略学习库")
        title.setObjectName("CardTitle")
        bl.addWidget(title)
        bl.addSpacing(6)
        bl.addWidget(QLabel("取前"))
        self.topn_cb = QComboBox()
        for n in (5, 10, 15, 20, 30, 50):
            self.topn_cb.addItem(f"{n} 只", n)
        self.topn_cb.setCurrentIndex(2)
        self.topn_cb.setFixedWidth(90)
        self.topn_cb.setToolTip("套用策略后生成的名单数量")
        bl.addWidget(self.topn_cb)
        self.btn_run = QPushButton("套用该策略生成名单")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self._run)
        bl.addWidget(self.btn_run)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.clicked.connect(self._export)
        bl.addWidget(self.btn_export)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(130)
        self.progress.setVisible(False)
        bl.addWidget(self.progress)
        bl.addStretch()
        self.state_lbl = QLabel("选择左侧策略，先学习再套用")
        self.state_lbl.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        bl.addWidget(self.state_lbl)
        root.addWidget(bar)

        hint = QLabel("说明：公开资料 ≠ 机构真实策略。本页仅将公开方法论映射到本系统 8 因子做近似选股。")
        hint.setObjectName("Hint")
        root.addWidget(hint)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        # 左侧策略列表
        left = QFrame()
        left.setObjectName("Card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 12)
        ll.setSpacing(6)
        cap = QLabel("策略库")
        cap.setObjectName("CardTitle")
        ll.addWidget(cap)
        self.list = QListWidget()
        self.list.setStyleSheet(
            "QListWidget { border: none; background: transparent; }"
            "QListWidget::item { padding: 9px 8px; border-radius: 6px; }"
            "QListWidget::item:selected { background: #FFF1F0; color: #C8161D; }")
        for s in lib.list_strategies():
            it = QListWidgetItem(s["name"])
            it.setToolTip(s["org"] + "\n" + s["tag"])
            self.list.addItem(it)
        self.list.currentRowChanged.connect(self._select_strategy)
        ll.addWidget(self.list, stretch=1)
        self.weight_lbl = QLabel("")
        self.weight_lbl.setWordWrap(True)
        self.weight_lbl.setStyleSheet(f"color:{TEXT2}; font-size:11px;")
        ll.addWidget(self.weight_lbl)
        left.setMinimumWidth(210)
        left.setMaximumWidth(300)
        split.addWidget(left)

        # 右侧：说明 / 结果 / 因子明细
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setHtml("<p style='color:#8A9099'>加载策略说明…</p>")
        right.addWidget(self.detail)

        self.tbl = make_table(
            ["排名", "代码", "名称", "行业", "综合分"] +
            [FACTOR_LABEL[k] for k in FACTOR_KEYS] +
            ["风险等级", "命中概念"], stretch_last=False)
        self.tbl.setColumnWidth(0, 46)
        self.tbl.setColumnWidth(1, 68)
        self.tbl.setColumnWidth(2, 88)
        self.tbl.setColumnWidth(3, 90)
        self.tbl.setColumnWidth(4, 62)
        for i in range(5, 5 + len(FACTOR_KEYS)):
            self.tbl.setColumnWidth(i, 50)
        self.tbl.setColumnWidth(13, 60)
        self.tbl.itemSelectionChanged.connect(self._show_reason)
        right.addWidget(self.tbl)

        reason = QFrame()
        reason.setObjectName("Card")
        rl = QVBoxLayout(reason)
        rl.setContentsMargins(12, 8, 12, 10)
        rc = QLabel("选中个股的因子构成与理由")
        rc.setObjectName("CardTitle")
        rl.addWidget(rc)
        self.reason = QTextBrowser()
        self.reason.setMinimumHeight(90)
        self.reason.setOpenExternalLinks(True)
        rl.addWidget(self.reason)
        right.addWidget(reason)

        right.setSizes([230, 300, 150])
        split.addWidget(right)
        split.setSizes([250, 880])
        root.addWidget(split, stretch=1)

        skeleton_table(self.tbl, 6, "尚未生成：点击“套用该策略生成名单”")

    # ---------------- 策略说明 ----------------
    def _current(self) -> dict:
        row = max(0, self.list.currentRow())
        return lib.list_strategies()[row]

    def _set_topn(self, n: int):
        """策略默认数量不在预设档位时自动补一档，保证显示与参数一致。"""
        idx = self.topn_cb.findData(n)
        if idx < 0:
            self.topn_cb.addItem(f"{n} 只", n)
            idx = self.topn_cb.count() - 1
        self.topn_cb.setCurrentIndex(idx)

    def _select_strategy(self, row: int):
        if row < 0:
            return
        s = lib.list_strategies()[row]
        self.detail.setHtml(render_strategy_html(s))
        self._set_topn(int((s.get("params") or {}).get("top_n", 15)))
        w = s.get("weights") or {}
        self.weight_lbl.setText("权重：" + " · ".join(
            f"{FACTOR_LABEL[k]}{w.get(k, 0):.0f}%" for k in FACTOR_KEYS))
        res = self._results.get(s["id"])
        if res is None:
            self.state_lbl.setText(f"已选择「{s['name']}」，可点击生成名单")
            skeleton_table(self.tbl, 6, "尚未生成：点击“套用该策略生成名单”")
            self.reason.clear()
        else:
            self._render_results(res)
            self.state_lbl.setText(
                f"「{s['name']}」上次结果：{len(res.get('picks', []))} 只 · "
                f"{res.get('generated_at', '')}")

    # ---------------- 生成 ----------------
    def _run(self):
        if self._busy:
            return
        s = self._current()
        self._busy = True
        self.btn_run.setEnabled(False)
        self.progress.setVisible(True)
        self.state_lbl.setText(f"正在套用「{s['name']}」打分…")
        self.set_status(f"策略学习：正在按「{s['name']}」多因子选股…")
        skeleton_table(self.tbl, 6, "正在打分…")

        params = s.get("params") or {}
        weights = lib.validate_weights(s.get("weights") or {})
        top_n = int(self.topn_cb.currentData())   # 主线程读取控件值，避免跨线程访问

        def task(p):
            snap = self.provider()
            if snap is None:
                p("市场信息尚未加载，先抓取当日信息（约 10~30 秒）…")
                try:
                    snap = market_info.refresh_all(progress=p)
                except Exception:
                    snap = market_info.load_latest_snapshot()
            p(f"按「{s['name']}」权重：趋势/量价/流动性/轮动/龙头/题材/资金/风险…")
            res = recommender.recommend(
                snap, top_n=top_n,
                max_candidates=int(params.get("max_candidates", 150)),
                weights=weights,
                industry_cap=int(params.get("industry_cap", 3)),
                min_score=float(params.get("min_score", 40)),
                progress=p)
            res["strategy_id"] = s["id"]
            res["strategy_name"] = s["name"]
            res["generated_at"] = dt.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S")
            return res

        w = Worker(task)
        w.progress.connect(self._on_progress)
        w.done.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_finished)
        self._workers.append(w)
        w.start()

    def _on_progress(self, msg: str):
        self.state_lbl.setText(msg)
        self.set_status(msg)

    def _on_finished(self):
        self._busy = False
        self.btn_run.setEnabled(True)
        self.progress.setVisible(False)
        self._workers = [x for x in self._workers if x.isRunning()]

    def _on_error(self, msg: str):
        self.state_lbl.setText("生成失败：" + msg)
        self.set_status("策略选股失败：" + msg)
        skeleton_table(self.tbl, 6, "生成失败，可重试")
        self._on_finished()

    def _on_done(self, res: dict):
        sid = res.get("strategy_id") or self._current()["id"]
        self._results[sid] = res
        self._render_results(res)
        self.set_status(
            f"「{res.get('strategy_name', '')}」完成：{len(res.get('picks', []))} 只，"
            f"候选 {res.get('candidates', 0)}，耗时 {res.get('elapsed', 0)}s")

    # ---------------- 渲染 ----------------
    def _render_results(self, res: dict):
        self._rows = {}
        self._meta = res.get("factors_meta") or {}
        picks = res.get("picks") or []
        quality = "数据降级" if res.get("degraded") else "数据完整"
        self.state_lbl.setText(
            f"{quality} · {len(picks)} 只 · 候选 {res.get('candidates', 0)} · "
            f"生成于 {res.get('generated_at', res.get('asof', ''))}")

        self.tbl.setSortingEnabled(False)
        self.tbl.setRowCount(0)
        for idx, p in enumerate(picks):
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            self._rows[r] = p
            self.tbl.setItem(r, 0, cell(
                p.get("rank", idx + 1), UP, bold=True, align=Qt.AlignCenter,
                sort_value=p.get("rank", idx + 1)))
            self.tbl.item(r, 0).setData(Qt.UserRole, p)
            self.tbl.setItem(r, 1, cell(p.get("code", ""), align=Qt.AlignCenter))
            self.tbl.setItem(r, 2, cell(p.get("name", ""), bold=True))
            self.tbl.setItem(r, 3, cell(p.get("industry", ""), TEXT2))
            score = float(p.get("score", 0) or 0)
            self.tbl.setItem(r, 4, cell(
                f"{score:.1f}", UP if score >= 65 else WARN,
                bold=True, align=Qt.AlignCenter, sort_value=score))
            fs = p.get("factor_scores") or {}
            for j, key in enumerate(FACTOR_KEYS):
                v = float(fs.get(key, 0) or 0)
                mx = float((self._meta.get(key) or {}).get("max", 1) or 1)
                ratio = v / mx if mx else 0
                col = UP if ratio >= 0.7 else (WARN if ratio >= 0.4 else TEXT2)
                self.tbl.setItem(r, 5 + j, cell(
                    f"{v:.0f}", col, align=Qt.AlignCenter, sort_value=v))
            risk = p.get("risk", "")
            self.tbl.setItem(r, 13, cell(
                risk, UP if risk == "高" else (WARN if risk == "中" else TEXT2),
                align=Qt.AlignCenter))
            self.tbl.setItem(r, 14, cell(
                "、".join(p.get("hit_concepts", [])[:5])))
        self.tbl.setSortingEnabled(True)
        # 显式按排名升序，避免默认降序把第 1 名排到表尾
        self.tbl.sortItems(0, Qt.AscendingOrder)
        if picks:
            self.tbl.selectRow(0)

    def _show_reason(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return
        item = self.tbl.item(rows[0].row(), 0)
        p = item.data(Qt.UserRole) if item else None
        if not p:
            return
        html = (f"<b style='color:{UP}'>#{p.get('rank', '')} "
                f"{p.get('code', '')} {_esc(p.get('name', ''))}</b> "
                f"<span style='color:{TEXT2}'>· {_esc(p.get('industry', ''))} "
                f"· 综合分 {float(p.get('score', 0) or 0):.1f}/100"
                f" · 风险{p.get('risk', '')}</span><br>")
        fs = p.get("factors") or {}
        html += "<table cellspacing='3' cellpadding='2'>"
        for key in FACTOR_KEYS:
            it = fs.get(key) or {}
            meta = self._meta.get(key) or {}
            sc = float(it.get("score", 0) or 0)
            mx = float(it.get("max", meta.get("max", 1)) or 1)
            ratio = sc / mx if mx else 0
            color = UP if ratio >= 0.7 else (WARN if ratio >= 0.4 else TEXT2)
            html += (f"<tr><td align='right'><b>{FACTOR_NAME(key)}</b></td>"
                     f"<td align='right'>{sc:.0f}/{mx:.0f}</td>"
                     f"<td style='color:{color}'>" + "█" * int(ratio * 10) +
                     f"</td><td>{_esc(it.get('note', ''))}</td></tr>")
        html += "</table>"
        if p.get("data_quality") == "降级":
            html += f"<span style='color:{WARN}'>数据降级：日线不可用，按快照估算</span><br>"
        for rr in p.get("reasons", []):
            html += f"• {_esc(rr)}<br>"
        self.reason.setHtml(html)

    # ---------------- 导出 ----------------
    def _export(self):
        s = self._current()
        res = self._results.get(s["id"])
        if not res or not res.get("picks"):
            self.set_status("当前策略暂无结果可导出")
            return
        default = os.path.join(
            os.path.expanduser("~"), "Desktop",
            f"策略选股_{s['id']}_{dt.date.today():%Y%m%d}.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出策略选股结果", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            n = export_table_csv(self.tbl, path)
            self.set_status(f"已导出 {n} 条到 {path}")
        except Exception as e:  # noqa
            self.set_status(f"导出失败：{e}")
