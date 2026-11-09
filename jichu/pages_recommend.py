# -*- coding: utf-8 -*-
"""
开盘前多因子荐股页面
====================
两个固定批次（均为开盘前，给足反应时间）：
  · 早盘批：工作日 9:27 自动生成（9:30 开盘前）
  · 午盘批：工作日 12:57 自动生成（13:00 下午开盘前）
支持手动生成；结果按批次本地留档，重启后可查看。

界面展示每只标的的因子构成（趋势/量价/流动性/轮动/龙头/题材/资金/风险），
选中后显示完整打分说明与命中理由；支持导出 CSV。
"""
import datetime as dt
import json
import os
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFrame, QSplitter, QTextBrowser,
                               QComboBox, QProgressBar, QFileDialog)

from ui_common import (UP, DOWN, FLAT, WARN, TEXT2, Worker, make_table, cell,
                       pct_color, skeleton_table, export_table_csv)
import market_info
import recommender

BATCHES = [("morning", "早盘批 · 9:27"), ("noon", "午盘批 · 12:57")]

FACTOR_KEYS = ["trend", "volume", "liquidity", "rotation",
               "leader", "theme", "capital", "risk"]
FACTOR_LABEL = {
    "trend": "趋势", "volume": "量价", "liquidity": "流动",
    "rotation": "轮动", "leader": "龙头", "theme": "题材",
    "capital": "资金", "risk": "风险",
}


class RecommendPage(QWidget):
    def __init__(self, set_status: Callable[[str], None],
                 snapshot_provider: Callable[[], Optional[object]],
                 on_snapshot: Callable[[object], None]):
        super().__init__()
        self.set_status = set_status
        self.provider = snapshot_provider
        self.on_snapshot = on_snapshot
        self.batch = "morning"
        self.results = {"morning": None, "noon": None}
        self._workers = []
        self._busy = False
        self._row_picks = {}
        self._build()
        self._load_cached()

    # ---------------- 缓存 ----------------
    def _cache_path(self, slot: str) -> str:
        return os.path.join(
            market_info.CACHE_DIR,
            f"picks_{slot}_{dt.date.today():%Y%m%d}.json")

    def _load_cached(self):
        for slot, _ in BATCHES:
            p = self._cache_path(slot)
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        self.results[slot] = json.load(f)
                except Exception:
                    self.results[slot] = None
        self._render()

    def _save_cached(self, slot: str, res: dict):
        tmp = self._cache_path(slot) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
        os.replace(tmp, self._cache_path(slot))

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        bar = QFrame()
        bar.setObjectName("Card")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 10)
        bl.setSpacing(10)

        # 批次选择：与其它页一致使用下拉框，单独一块，与操作按钮区分
        bl.addWidget(QLabel("推荐批次"))
        self.batch_cb = QComboBox()
        for slot, label in BATCHES:
            self.batch_cb.addItem(label, slot)
        self.batch_cb.setMinimumWidth(150)
        self.batch_cb.setToolTip("早盘批 9:27 生成；午盘批 12:57 使用上午行情重排")
        self.batch_cb.currentIndexChanged.connect(self._on_batch_changed)
        bl.addWidget(self.batch_cb)
        divider = QFrame()
        divider.setFixedWidth(1)
        divider.setFixedHeight(24)
        divider.setStyleSheet("background:#E3E6EA;")
        bl.addWidget(divider)
        self.btn = QPushButton("生成多因子荐股")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.generate)
        bl.addWidget(self.btn)
        self.export_btn = QPushButton("导出 CSV")
        self.export_btn.clicked.connect(self._export)
        bl.addWidget(self.export_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(140)
        self.progress.setVisible(False)
        bl.addWidget(self.progress)
        bl.addStretch()
        root.addWidget(bar)

        # 状态行：说明文字 + 降级提示 + 生成时间（放第二行，避免顶栏被长文本撑宽）
        info_row = QHBoxLayout()
        info_row.setSpacing(10)
        self.note_lbl = QLabel("开盘前 T-3 分钟生成；非交易时段使用最近收盘数据并自动降级")
        self.note_lbl.setObjectName("Hint")
        self.note_lbl.setWordWrap(True)
        self.degraded_lbl = QLabel("")
        self.degraded_lbl.setStyleSheet(f"color:{WARN}; font-size:12px;")
        self.date_lbl = QLabel("")
        self.date_lbl.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        info_row.addWidget(self.note_lbl, 1)
        info_row.addWidget(self.degraded_lbl)
        info_row.addWidget(self.date_lbl)
        root.addLayout(info_row)

        # 荐股表占满整行宽度，避免右侧梯队挤压主要数据列
        headers = ["排名", "代码", "名称", "行业", "现价", "涨幅%", "综合分"] + \
                  [FACTOR_LABEL[k] for k in FACTOR_KEYS] + \
                  ["命中概念", "PE/PB", "60日/年内%"]
        self.pick_tbl = make_table(headers, stretch_last=False)
        self.pick_tbl.setColumnWidth(0, 40)
        self.pick_tbl.setColumnWidth(1, 60)
        self.pick_tbl.setColumnWidth(2, 76)
        self.pick_tbl.setColumnWidth(3, 68)
        for i in range(4, 7):
            self.pick_tbl.setColumnWidth(i, 56)
        for i in range(7, 7 + len(FACTOR_KEYS)):
            self.pick_tbl.setColumnWidth(i, 42)
        self.pick_tbl.setColumnWidth(15, 130)
        self.pick_tbl.setColumnWidth(16, 72)
        self.pick_tbl.setColumnWidth(17, 92)
        self.pick_tbl.setMinimumHeight(190)
        self.pick_tbl.itemSelectionChanged.connect(self._show_pick_reason)
        root.addWidget(self.pick_tbl, stretch=3)

        # 底部：涨停梯队 + 因子与推荐理由，左右并排，不再是主表右侧的挤压块
        bottom = QSplitter(Qt.Horizontal)
        bottom.setChildrenCollapsible(False)

        ladder_card = QFrame()
        ladder_card.setObjectName("Card")
        ll = QVBoxLayout(ladder_card)
        ll.setContentsMargins(12, 10, 12, 10)
        ll.setSpacing(6)
        cap1 = QLabel("涨停梯队（连板 / 封单 / 炸板）")
        cap1.setObjectName("CardTitle")
        cap1.setToolTip("用于判断市场情绪高度与接力风险：连板高度、封单金额、炸板次数")
        ll.addWidget(cap1)
        self.ladder_tbl = make_table(
            ["代码", "名称", "连板", "封单(亿)", "炸板", "涨停原因"])
        self.ladder_tbl.setColumnWidth(0, 60)
        self.ladder_tbl.setColumnWidth(1, 80)
        self.ladder_tbl.setColumnWidth(2, 46)
        self.ladder_tbl.setColumnWidth(3, 64)
        self.ladder_tbl.setColumnWidth(4, 46)
        self.ladder_tbl.setColumnWidth(5, 180)
        ll.addWidget(self.ladder_tbl)
        bottom.addWidget(ladder_card)

        reason_card = QFrame()
        reason_card.setObjectName("Card")
        rl = QVBoxLayout(reason_card)
        rl.setContentsMargins(12, 10, 12, 10)
        rl.setSpacing(6)
        cap = QLabel("因子构成与推荐理由")
        cap.setObjectName("CardTitle")
        rl.addWidget(cap)
        self.reason_box = QTextBrowser()
        self.reason_box.setMinimumHeight(110)
        self.reason_box.setOpenExternalLinks(True)
        rl.addWidget(self.reason_box)
        bottom.addWidget(reason_card)

        bottom.setSizes([620, 480])
        bottom.setMinimumHeight(180)
        bottom.setMaximumHeight(250)
        root.addWidget(bottom, stretch=0)

        skeleton_table(self.pick_tbl, 8, "等待生成多因子名单…")
        skeleton_table(self.ladder_tbl, 6, "等待涨停梯队…")

    def _on_batch_changed(self, idx: int):
        slot = self.batch_cb.itemData(idx)
        if slot and slot != self.batch:
            self._switch_batch(slot)

    def _switch_batch(self, slot: str):
        self.batch = slot
        self._render()

    # ---------------- 生成 ----------------
    def generate(self):
        self._run(self.batch, manual=True)

    def auto_generate(self, slot: str):
        """定时任务入口：切到对应批次并自动生成。"""
        self.batch = slot
        self._run(slot, manual=False)

    def has_result_today(self, slot: str) -> bool:
        """当天该批次是否已有结果（自动任务据此避免重复抓取）。"""
        return self.results.get(slot) is not None

    def _run(self, slot: str, manual: bool):
        if self._busy:
            return
        self._busy = True
        self.btn.setEnabled(False)
        self.progress.setVisible(True)
        kind = "手动" if manual else "自动"
        label = dict(BATCHES)[slot]
        self.set_status(f"正在{kind}生成{label}多因子荐股…")
        skeleton_table(self.pick_tbl, 8, "正在多因子打分…")

        def task(p):
            snap = self.provider()
            # 午盘批必须使用当日 11:30 的上午收盘行情与板块数据，因此强制刷新
            if slot == "noon":
                p("午盘批：重新抓取上午收盘行情与板块强弱…")
                snap = market_info.refresh_all(progress=p, force=True)
                self.on_snapshot(snap)
            elif snap is None:
                p("信息尚未加载，先抓取最新消息…")
                snap = market_info.refresh_all(progress=p)
                self.on_snapshot(snap)
            if slot == "noon":
                p("正在按「上午强度 + 板块排名 + 半日量能」重排午后推荐…")
            else:
                p("正在结合当日信息与题材打分（趋势/量价/轮动/龙头/题材/风险）…")
            res = recommender.recommend(snap, progress=p, session=slot)
            res["slot"] = slot
            res["manual"] = manual
            res["generated_at"] = dt.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S")
            return res

        w = Worker(task)
        w.progress.connect(self.set_status)
        w.done.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_finished)
        self._workers.append(w)
        w.start()

    def _on_finished(self):
        self._busy = False
        self.btn.setEnabled(True)
        self.progress.setVisible(False)
        self._workers = [x for x in self._workers if x.isRunning()]

    def _on_error(self, msg):
        self.set_status("荐股失败：" + msg)
        self._on_finished()

    def _on_done(self, res):
        slot = res.get("slot", self.batch)
        self.results[slot] = res
        self.batch = slot
        self._save_cached(slot, res)
        self._render()
        self.set_status(
            f"{dict(BATCHES)[slot]}完成：推荐 {len(res.get('picks', []))} 只，"
            f"涨停梯队 {len(res.get('ladder', []))} 只，"
            f"耗时 {res.get('elapsed', 0)}s")

    # ---------------- 渲染 ----------------
    def _render(self):
        idx = 1 if self.batch == "noon" else 0
        self.batch_cb.blockSignals(True)
        self.batch_cb.setCurrentIndex(idx)
        self.batch_cb.blockSignals(False)
        res = self.results[self.batch]
        self._row_picks = {}
        if res is None:
            self.date_lbl.setText("本批次尚未生成")
            self.degraded_lbl.setText("")
            self.note_lbl.setText(
                "开盘前 T-3 分钟生成；非交易时段使用最近收盘数据并自动降级")
            self.reason_box.clear()
            skeleton_table(self.pick_tbl, 8, "点击“生成多因子荐股”开始")
            skeleton_table(self.ladder_tbl, 6, "等待涨停梯队…")
            return

        tag = "手动" if res.get("manual") else "自动"
        self.date_lbl.setText(
            f"[{tag}] 交易日 {res.get('trade_date', '')} · "
            f"生成于 {res.get('generated_at', res.get('asof', ''))}")
        self.degraded_lbl.setText(
            "⚠ 数据降级" if res.get("degraded") else "")
        notes = res.get("data_notes") or []
        extra = (f" · 全市场 {res.get('universe', 0)} 只 / "
                 f"候选 {res.get('candidates', 0)} 只")
        brief = res.get("session_brief") or ""
        if brief:
            self.note_lbl.setText(brief + extra)
            self.note_lbl.setToolTip("；".join(notes))
        else:
            self.note_lbl.setText(("；".join(notes) if notes else
                                   "数据完整：行情 + 日线 + 板块轮动 + 题材") + extra)

        self.pick_tbl.setSortingEnabled(False)
        self.pick_tbl.setRowCount(0)
        for p in res.get("picks", []):
            r = self.pick_tbl.rowCount()
            self.pick_tbl.insertRow(r)
            self._row_picks[r] = p
            self.pick_tbl.setItem(r, 0, cell(
                p.get("rank", r + 1), UP, bold=True, align=Qt.AlignCenter,
                sort_value=p.get("rank", r + 1)))
            c0 = self.pick_tbl.item(r, 0)
            c0.setData(Qt.UserRole, p)
            self.pick_tbl.setItem(r, 1, cell(
                p.get("code", ""), align=Qt.AlignCenter))
            self.pick_tbl.setItem(r, 2, cell(p.get("name", ""), bold=True))
            self.pick_tbl.setItem(r, 3, cell(p.get("industry", ""), TEXT2))
            self.pick_tbl.setItem(r, 4, cell(
                f"{p.get('price', 0):.2f}", align=Qt.AlignCenter,
                sort_value=p.get("price", 0)))
            self.pick_tbl.setItem(r, 5, cell(
                f"{p.get('change_pct', 0):+.2f}",
                pct_color(p.get("change_pct", 0)), align=Qt.AlignCenter,
                sort_value=p.get("change_pct", 0)))
            score = float(p.get("score", 0))
            sc_color = UP if score >= 65 else (WARN if score >= 50 else TEXT2)
            self.pick_tbl.setItem(r, 6, cell(
                f"{score:.1f}", sc_color, bold=True, align=Qt.AlignCenter,
                sort_value=score))

            fs = p.get("factor_scores") or {}
            for j, key in enumerate(FACTOR_KEYS):
                v = float(fs.get(key, 0) or 0)
                meta = recommender.FACTOR_META.get(key, {})
                mx = meta.get("max", 1)
                ratio = v / mx if mx else 0
                col = UP if ratio >= 0.7 else (WARN if ratio >= 0.4 else TEXT2)
                self.pick_tbl.setItem(r, 7 + j, cell(
                    f"{v:.0f}", col, align=Qt.AlignCenter, sort_value=v))
            self.pick_tbl.setItem(
                r, 7 + len(FACTOR_KEYS),
                cell("、".join(p.get("hit_concepts", [])[:5])))
            pe = float(p.get("pe") or 0)
            pb = float(p.get("pb") or 0)
            pe_txt = f"{pe:.1f}" if pe > 0 else "亏损"
            pb_txt = f"{pb:.2f}" if pb > 0 else "—"
            self.pick_tbl.setItem(r, 16, cell(
                f"{pe_txt}/{pb_txt}",
                (WARN if pe > 60 else TEXT2) if pe > 0 else DOWN,
                align=Qt.AlignCenter, sort_value=pe))
            v60 = float(p.get("chg60") or 0)
            vytd = float(p.get("chg_ytd") or 0)
            self.pick_tbl.setItem(r, 17, cell(
                f"{v60:+.1f}/{vytd:+.1f}", pct_color(v60),
                align=Qt.AlignCenter, sort_value=v60))

        self.ladder_tbl.setRowCount(0)
        for s in res.get("ladder", []):
            r = self.ladder_tbl.rowCount()
            self.ladder_tbl.insertRow(r)
            self.ladder_tbl.setItem(r, 0, cell(
                s.get("code", ""), align=Qt.AlignCenter))
            self.ladder_tbl.setItem(r, 1, cell(s.get("name", ""), bold=True))
            self.ladder_tbl.setItem(r, 2, cell(
                s.get("boards", 0), UP, bold=True, align=Qt.AlignCenter,
                sort_value=s.get("boards", 0)))
            self.ladder_tbl.setItem(r, 3, cell(
                f"{s.get('seal_fund', 0):.2f}", align=Qt.AlignCenter,
                sort_value=s.get("seal_fund", 0)))
            self.ladder_tbl.setItem(r, 4, cell(
                s.get("break_count", 0), align=Qt.AlignCenter,
                sort_value=s.get("break_count", 0)))
            self.ladder_tbl.setItem(
                r, 5, cell("、".join(s.get("reasons", []))))

        self.pick_tbl.setSortingEnabled(True)
        self.ladder_tbl.setSortingEnabled(True)
        if res.get("picks"):
            self.pick_tbl.selectRow(0)

    # ---------------- 实时行情（全局被动刷新，3 秒） ----------------
    def live_codes(self):
        return [str(p.get("code")) for p in self._row_picks.values()
                if p.get("code")]

    def apply_live(self, quotes: dict):
        if not self._row_picks:
            return
        for row, p in self._row_picks.items():
            if row >= self.pick_tbl.rowCount():
                continue
            q = (quotes or {}).get(str(p.get("code")))
            if not q or not q.get("price"):
                continue
            px = float(q["price"])
            chg = float(q.get("change_pct") or 0)
            p["price"], p["change_pct"] = px, chg
            c4 = self.pick_tbl.item(row, 4)
            if c4 is not None:
                c4.setText(f"{px:.2f}")
            c5 = self.pick_tbl.item(row, 5)
            if c5 is not None:
                c5.setText(f"{chg:+.2f}")
                c5.setForeground(QColor(pct_color(chg)))

    def _show_pick_reason(self):
        rows = self.pick_tbl.selectionModel().selectedRows()
        if not rows:
            return
        item = self.pick_tbl.item(rows[0].row(), 0)
        p = item.data(Qt.UserRole) if item else None
        if not p:
            return
        name = p.get("name", "")
        html = ""
        brief = (self.results.get(self.batch) or {}).get("session_brief") or ""
        if brief:
            html += (f"<span style='color:{TEXT2}'>{brief}</span>"
                     f"<hr style='border:none;border-top:1px solid #E6E8EB'>")
        html += (f"<b style='color:{UP}'>#{p.get('rank', '')} "
                 f"{p.get('code', '')} {name}</b> "
                 f"<span style='color:{TEXT2}'>{p.get('industry', '')} · "
                 f"{p.get('market', '')}</span><br>")
        html += f"<b>综合分 {float(p.get('score', 0)):.1f} / 100</b>"
        bonus = float(p.get("session_bonus", 0) or 0)
        if bonus:
            html += f" · 午盘修正 {bonus:+.1f}"
        if p.get("leader_flag"):
            html += f" · <b>{p['leader_flag']}</b>"
        html += (f" · 风险等级 <span style='color:"
                 f"{UP if p.get('risk') == '高' else TEXT2}'>{p.get('risk', '')}"
                 f"</span>")
        if p.get("tags"):
            html += f"<br><span style='color:{WARN}'>" + \
                    " / ".join(p["tags"]) + "</span>"
        html += "<br>"

        # 因子条
        fs = p.get("factors") or {}
        html += "<table cellspacing='4' cellpadding='2'>"
        for key in FACTOR_KEYS:
            item = fs.get(key) or {}
            meta = recommender.FACTOR_META.get(key, {})
            sc = float(item.get("score", 0) or 0)
            mx = float(item.get("max", meta.get("max", 1)) or 1)
            ratio = sc / mx if mx else 0
            color = UP if ratio >= 0.7 else (WARN if ratio >= 0.4 else TEXT2)
            bar = "█" * int(ratio * 10)
            html += (f"<tr><td align='right'><b>{meta.get('name', key)}</b></td>"
                     f"<td align='right'>{sc:.0f}/{mx:.0f}</td>"
                     f"<td style='color:{color}'>{bar}</td>"
                     f"<td>{item.get('note', '')}</td></tr>")
        html += "</table>"
        m = p.get("morning") or {}
        if m:
            html += (f"<b>上午盘面：</b>涨 {m.get('change_pct', 0):+.1f}% · "
                     f"成交额 {m.get('amount', 0):.2f} 亿 · "
                     f"换手 {m.get('turnover', 0):.1f}% · "
                     f"{m.get('sector', '')} {m.get('sector_pct', 0):+.1f}%"
                     + (f"（{m.get('stage')}）" if m.get("stage") else "")
                     + f" · 全市场上涨占比 {m.get('up_ratio', 0.5):.0%}<br>")
        if p.get("data_quality") == "降级":
            html += (f"<span style='color:{WARN}'>数据降级：日线不可用，"
                     f"因子按行情快照估算</span><br>")
        html += "<br>"
        for rr in p.get("reasons", []):
            html += f"• {rr}<br>"
        self.reason_box.setHtml(html)

    # ---------------- 导出 ----------------
    def _export(self):
        res = self.results.get(self.batch)
        if not res or not res.get("picks"):
            self.set_status("当前批次暂无结果可导出")
            return
        default = os.path.join(
            os.path.expanduser("~"), "Desktop",
            f"多因子荐股_{self.batch}_{dt.date.today():%Y%m%d}.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出荐股结果", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            n = export_table_csv(self.pick_tbl, path)
            self.set_status(f"已导出 {n} 条到 {path}")
        except Exception as e:  # noqa
            self.set_status(f"导出失败：{e}")
