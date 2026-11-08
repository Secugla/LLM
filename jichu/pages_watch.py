# -*- coding: utf-8 -*-
"""
个股自选页面
============
自选股管理、买卖信号提示、多空信号明细、仓位建议。

本地持久化（纯本地运行）：
  · 自选清单 watchlist.json：启动立即渲染，加入/移除原子写入，重启不丢失
  · 信号缓存 .cache/watch_signals.json：启动先显示上次信号，再后台重算覆盖
即使网络失败，自选代码与名称也一定显示。
"""
import json
import os
from typing import Callable, List

import requests
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFrame, QLineEdit, QSplitter,
                               QTextBrowser, QProgressBar, QHeaderView)

from ui_common import (UP, DOWN, FLAT, TEXT, TEXT2, Worker, make_table,
                       cell, pct_color, skeleton_table)
import signal_engine

WATCH_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
SIGNAL_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache",
    "watch_signals.json")

_SIG_COLOR = {"强烈买入": UP, "买入": UP, "观望": FLAT,
              "卖出": DOWN, "强烈卖出": DOWN}


class WatchlistPage(QWidget):
    def __init__(self, set_status: Callable[[str], None]):
        super().__init__()
        self.set_status = set_status
        self.items: List[dict] = []
        self.signals: List[dict] = []
        self._workers = []
        self._load()
        self._build()
        # 1. 先用本地清单立即渲染（代码/名称一定在）
        self._render_placeholder()
        # 2. 有缓存信号则立即显示
        self._load_cached_signals()
        # 3. 最新信号不在此立即拉取，由主窗口在信息页刷新完成后错峰调用
        #    start_auto_refresh()，避免启动瞬间多个线程池同时抢网络触发限速

    def start_auto_refresh(self):
        """信息页刷新完成后由主窗口调用，错峰重算最新信号。"""
        if self.items:
            self.refresh_signals()

    # ---------- 清单持久化（原子写入） ----------
    def _load(self):
        if os.path.exists(WATCH_FILE):
            try:
                with open(WATCH_FILE, "r", encoding="utf-8") as f:
                    self.items = json.load(f).get("items", [])
            except Exception:
                self.items = []

    def _save(self):
        tmp = WATCH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self.items}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, WATCH_FILE)

    # ---------- 信号缓存 ----------
    def _load_cached_signals(self):
        if not os.path.exists(SIGNAL_CACHE):
            return
        try:
            with open(SIGNAL_CACHE, "r", encoding="utf-8") as f:
                sigs = json.load(f).get("signals", [])
            # 只保留仍在自选清单内的信号
            codes = {x["code"] for x in self.items}
            self.signals = [s for s in sigs if s["code"] in codes]
            if self.signals:
                self._fill_table(self.signals)
        except Exception:
            pass

    def _save_cached_signals(self, sigs: List[dict]):
        try:
            tmp = SIGNAL_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"signals": sigs}, f, ensure_ascii=False)
            os.replace(tmp, SIGNAL_CACHE)
        except Exception:
            pass

    # ---------- UI ----------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        bar = QFrame()
        bar.setObjectName("Card")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(14, 10, 14, 10)
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入6位股票代码，如 601566")
        self.input.setMaximumWidth(200)
        self.input.returnPressed.connect(self.add)
        add_btn = QPushButton("加入自选")
        add_btn.clicked.connect(self.add)
        del_btn = QPushButton("移除选中")
        del_btn.clicked.connect(self.remove)
        self.refresh_btn = QPushButton("刷新买卖信号")
        self.refresh_btn.setObjectName("Primary")
        self.refresh_btn.clicked.connect(self.refresh_signals)
        bl.addWidget(QLabel("自选："))
        bl.addWidget(self.input)
        bl.addWidget(add_btn)
        bl.addWidget(del_btn)
        bl.addStretch()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(140)
        self.progress.setVisible(False)
        bl.addWidget(self.progress)
        bl.addWidget(self.refresh_btn)
        root.addWidget(bar)

        split = QSplitter(Qt.Vertical)
        self.tbl = make_table(
            ["代码", "名称", "现价", "信号", "置信度", "多空分",
             "RSI", "量比", "近5日%", "近20日%"], stretch_last=False)
        self.tbl.setColumnWidth(0, 70)
        self.tbl.setColumnWidth(1, 100)
        self.tbl.setColumnWidth(3, 90)
        # 列数少且内容短：平均铺满卡片宽度，避免表格右侧出现大片空白
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.itemSelectionChanged.connect(self._show_detail)
        split.addWidget(self.tbl)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 12)
        cap = QLabel("信号明细与仓位建议")
        cap.setObjectName("CardTitle")
        cl.addWidget(cap)
        self.detail = QTextBrowser()
        self.detail.setHtml(
            f'<div style="color:{TEXT2}; font-size:12px; padding:10px 4px">'
            '选择上方任意标的，查看信号构成、仓位建议与风险提示</div>')
        cl.addWidget(self.detail)
        split.addWidget(card)
        split.setSizes([360, 260])
        root.addWidget(split, stretch=3)

    # ---------- 快速名称（腾讯单只，akshare 兜底） ----------
    def _quick_name(self, code: str) -> str:
        prefix = "sh" if code.startswith(("6", "9")) else (
            "bj" if code.startswith(("8", "4")) else "sz")
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={prefix}{code}",
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=(4, 8))
            r.encoding = "gbk"
            f = r.text.split('"')[1].split("~")
            if len(f) > 1 and f[1]:
                return f[1]
        except Exception:
            pass
        # 名称解析失败时不再走 akshare（其代码表首包约 30 秒，会卡住界面）
        return ""

    # ---------- 操作 ----------
    def add(self):
        code = self.input.text().strip()
        if not (len(code) == 6 and code.isdigit()):
            self.set_status("请输入6位数字代码")
            return
        if any(x["code"] == code for x in self.items):
            self.set_status("该股票已在自选中")
            return
        name = self._quick_name(code)
        self.items.append({"code": code, "name": name})
        self._save()
        self.input.clear()
        self.set_status(f"已加入自选：{code} {name}")
        self.refresh_signals()

    def remove(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return
        codes = {self.tbl.item(r.row(), 0).text() for r in rows}
        self.items = [x for x in self.items if x["code"] not in codes]
        self._save()
        self.signals = [s for s in self.signals if s["code"] not in codes]
        self.set_status("已移除选中自选股")
        if self.items:
            self.refresh_signals()
        else:
            self.tbl.setRowCount(0)
            self.detail.clear()

    def refresh_signals(self):
        if not self.items:
            self.tbl.setRowCount(0)
            self.detail.clear()
            return
        self.refresh_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.set_status("正在计算自选股买卖信号…")
        if not self.signals:
            skeleton_table(self.tbl, max(2, len(self.items)), "正在计算信号…")
        items = [dict(x) for x in self.items]
        w = Worker(lambda p: signal_engine.analyze_watchlist(items))
        w.done.connect(self._on_signals)
        w.error.connect(lambda m: self.set_status("信号计算失败：" + m))
        w.finished.connect(self._on_signals_finished)
        self._workers.append(w)
        w.start()

    def _on_signals_finished(self):
        self.refresh_btn.setEnabled(True)
        self.progress.setVisible(False)
        self._workers = [x for x in self._workers if x.isRunning()]

    def _on_signals(self, sigs):
        self.signals = sigs
        if sigs:
            self._save_cached_signals(sigs)
            self._fill_table(sigs)
        else:
            # 网络全部失败时保留占位/缓存，不清空，并提示
            self.set_status("实时信号获取失败（网络受限），当前显示本地已有数据")
            self.refresh_btn.setEnabled(True)

    # ---------- 表格渲染 ----------
    def _render_placeholder(self):
        """只渲染本地清单（代码/名称），其余列占位。"""
        self.tbl.setSortingEnabled(False)
        self.tbl.setRowCount(0)
        for it in self.items:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            self.tbl.setItem(r, 0, cell(it["code"], align=Qt.AlignCenter))
            self.tbl.setItem(r, 1, cell(it.get("name", "")))
            for c in range(2, 10):
                self.tbl.setItem(r, c, cell("—", TEXT2, align=Qt.AlignCenter))

    def _fill_table(self, sigs: List[dict]):
        self.tbl.setSortingEnabled(False)
        self.tbl.setRowCount(0)
        for s in sigs:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            self.tbl.setItem(r, 0, cell(s["code"], align=Qt.AlignCenter))
            self.tbl.item(r, 0).setData(Qt.UserRole, s)
            self.tbl.setItem(r, 1, cell(s["name"], bold=True))
            self.tbl.setItem(r, 2, cell(s["price"], align=Qt.AlignCenter,
                                         sort_value=s["price"]))
            sig = s["signal"]
            self.tbl.setItem(r, 3, cell(sig, _SIG_COLOR.get(sig, FLAT),
                                         bold=True, align=Qt.AlignCenter))
            self.tbl.setItem(r, 4, cell(
                f"{s['confidence']:.0%}", align=Qt.AlignCenter,
                sort_value=s["confidence"]))
            self.tbl.setItem(r, 5, cell(f"{s['score']:+.1f}",
                                         pct_color(s["score"]),
                                         align=Qt.AlignCenter,
                                         sort_value=s["score"]))
            ind = s["indicators"]
            self.tbl.setItem(r, 6, cell(ind["rsi"], align=Qt.AlignCenter,
                                         sort_value=ind["rsi"]))
            self.tbl.setItem(r, 7, cell(ind["vol_ratio"],
                                         align=Qt.AlignCenter,
                                         sort_value=ind["vol_ratio"]))
            self.tbl.setItem(r, 8, cell(f"{ind['ret5']:+.1f}",
                                        pct_color(ind["ret5"]),
                                        align=Qt.AlignCenter,
                                        sort_value=ind["ret5"]))
            self.tbl.setItem(r, 9, cell(f"{ind['ret20']:+.1f}",
                                        pct_color(ind["ret20"]),
                                        align=Qt.AlignCenter,
                                        sort_value=ind["ret20"]))
        self.tbl.setSortingEnabled(True)
        self.set_status(f"自选信号已刷新：{len(sigs)} 只")
        self.tbl.selectRow(0)

    def _show_detail(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return
        item = self.tbl.item(rows[0].row(), 0)
        s = item.data(Qt.UserRole) if item else None
        if not s:
            return
        sig = s["signal"]
        ind = s["indicators"]
        ma = ind["ma"]
        html = f"<b style='font-size:14px;color:{_SIG_COLOR.get(sig, TEXT)}'>"
        html += f"{s['code']} {s['name']} → {sig}</b>"
        html += f"（置信度 {s['confidence']:.0%}，多空总分 {s['score']:+.1f}）<br>"
        html += f"MA5 {ma['5']} · MA20 {ma['20']} · MA60 {ma['60']}<br>"
        html += f"DIF {ind['dif']} · DEA {ind['dea']} · MACD柱 {ind['hist']}<br>"
        html += f"RSI {ind['rsi']} · 量比 {ind['vol_ratio']} · "
        html += f"布林[{ind['boll_dn']}, {ind['boll_up']}]<br>"
        html += "<hr style='border:none;border-top:1px solid #E6E8EB'>"
        html += f"<b style='color:{UP}'>多头信号：</b><br>"
        for b in s["bull"]:
            html += f"<span style='color:{UP}'>+ {b}</span><br>"
        html += f"<b style='color:{DOWN}'>空头信号：</b><br>"
        for b in s["bear"]:
            html += f"<span style='color:{DOWN}'>- {b}</span><br>"
        html += "<b>操作建议：</b>" + s["advice"]
        self.detail.setHtml(html)
