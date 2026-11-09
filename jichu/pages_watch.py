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
import datetime as dt
import json
import os
from typing import Callable, List

import numpy as np
import pyqtgraph as pg
import requests
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFrame, QLineEdit, QSplitter,
                               QTextBrowser, QProgressBar, QHeaderView, QDialog,
                               QCheckBox)

from ui_common import (UP, DOWN, FLAT, TEXT, TEXT2, CARD, BORDER, Worker,
                       make_table, cell, pct_color, skeleton_table)
import data_sources
import signal_engine

WATCH_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
SIGNAL_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache",
    "watch_signals.json")

_SIG_COLOR = {"强烈买入": UP, "买入": UP, "观望": FLAT,
              "卖出": DOWN, "强烈卖出": DOWN}

# 分时窗口可能在取数未完成时被关闭：这里持有 QThread 引用，避免线程运行中被回收
_LIVE_WORKERS = set()


class _TickAxis(pg.AxisItem):
    """把整数索引映射为自定义刻度文本（时间或日期）。"""

    def __init__(self, labels):
        super().__init__(orientation="bottom")
        self.labels = labels

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            out.append(self.labels[i] if 0 <= i < len(self.labels) else "")
        return out


def _signal_history(df, days: int = 60) -> list:
    """按自选信号同一套日线规则回溯最近的买入/卖出信号。

    返回 [(日期, 信号等级, 总分)]，仅含买入/卖出类信号（不含观望）。
    """
    events = []
    start = max(60, len(df) - days)
    for i in range(start, len(df)):
        sub = df.iloc[:i + 1]
        try:
            ind = signal_engine.compute_indicators(sub)
            total, _, _ = signal_engine.score_signals(ind)
            grade, _ = signal_engine._grade(total)
        except Exception:
            continue
        if grade in ("强烈买入", "买入", "卖出", "强烈卖出"):
            events.append((sub.index[-1].strftime("%Y-%m-%d"),
                           grade, round(float(total), 2)))
    return events


class IntradayDialog(QDialog):
    """个股分时窗口：近两日 5 分钟 K 线 + 当日实时快照 + 近 90 日买卖信号标注。"""

    def __init__(self, code: str, name: str, parent=None):
        super().__init__(parent)
        self.code, self.name = code, name
        self._workers = []
        self.setWindowTitle(f"{code} {name} · 分时与买卖点")
        self.resize(1000, 760)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.quote_lbl = QLabel("")
        self.quote_lbl.setStyleSheet(
            f"color:{TEXT}; font-size:15px; font-weight:600;")
        self.info = QLabel("正在加载分时与买卖点…")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.reload_btn = QPushButton("重新加载")
        self.reload_btn.clicked.connect(self._load)
        top.addWidget(self.quote_lbl)
        top.addWidget(self.info, 1)
        top.addWidget(self.reload_btn)
        lay.addLayout(top)
        self.gw = pg.GraphicsLayoutWidget()
        self.gw.setStyleSheet(f"border:1px solid {BORDER}; border-radius:6px;")
        lay.addWidget(self.gw, 1)
        # 盘中每 3 秒刷新现价（被动刷新，与主窗口同一节奏）
        self._quote_busy = False
        self._price_line = None
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._auto_tick)
        self._timer.start()
        self._load()

    # ---------- 取数 ----------
    def _load(self):
        self.reload_btn.setEnabled(False)
        self.info.setText("正在加载分时与买卖点…")
        self.gw.clear()
        w = Worker(self._fetch)
        w.done.connect(self._render)
        w.error.connect(self._on_error)
        w.finished.connect(self._finished)
        _LIVE_WORKERS.add(w)
        w.finished.connect(lambda: _LIVE_WORKERS.discard(w))
        self._workers.append(w)
        w.start()

    def _fetch(self, _progress=None):
        end = dt.date.today()
        m_start = (end - dt.timedelta(days=14)).strftime("%Y%m%d")
        d_start = (end - dt.timedelta(days=460)).strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        out = {"snap": None, "minute": None, "daily": None, "err": []}
        try:
            out["snap"] = data_sources.fetch_realtime_snapshot(self.code)
        except Exception as ex:  # noqa
            out["err"].append(f"实时快照失败：{ex}")
        try:
            out["minute"] = data_sources.fetch_minute(
                self.code, m_start, e, "5")
        except Exception as ex:  # noqa
            out["err"].append(f"分钟线失败：{ex}")
        try:
            out["daily"] = data_sources.fetch_daily_fast(self.code, d_start, e)
        except Exception as ex:  # noqa
            out["err"].append(f"日线失败：{ex}")
        return out

    def _finished(self):
        self.reload_btn.setEnabled(True)
        self._workers = [w for w in self._workers if w.isRunning()]

    def _on_error(self, msg):
        self.info.setText(f"加载失败：{msg}")

    def _auto_tick(self):
        """盘中每 3 秒刷新一次现价与信息条（不重画整张图）。"""
        if not self.isVisible() or self._quote_busy:
            return
        now = dt.datetime.now()
        if now.weekday() >= 5:
            return
        t = now.hour * 100 + now.minute
        if not ((930 <= t <= 1130) or (1300 <= t <= 1500)):
            return
        self._quote_busy = True
        w = Worker(lambda _p: data_sources.fetch_realtime_snapshot(self.code))
        w.done.connect(self._apply_tick)
        w.error.connect(self._tick_failed)
        _LIVE_WORKERS.add(w)
        w.finished.connect(lambda: _LIVE_WORKERS.discard(w))
        self._workers.append(w)
        w.start()

    def _tick_failed(self, _msg=""):
        self._quote_busy = False

    def _apply_tick(self, snap):
        self._quote_busy = False
        if not snap or not snap.get("price"):
            return
        px = float(snap["price"])
        chg = float(snap.get("change_pct") or 0)
        tm = snap.get("quote_time") or ""
        self.quote_lbl.setText(f"{px:.2f}  {chg:+.2f}%  {tm}")
        self.quote_lbl.setStyleSheet(
            f"color:{pct_color(chg)}; font-size:15px; font-weight:600;")
        if self._price_line is not None:
            self._price_line.setPos(px)
            try:
                self._price_line.label.setText(f"现价 {px:.2f}")
            except Exception:
                pass

    # ---------- 渲染 ----------
    def _render(self, data):
        snap = data.get("snap") or {}
        minute, daily = data.get("minute"), data.get("daily")
        events = (_signal_history(daily)
                  if daily is not None and len(daily) >= 61 else [])

        nm = snap.get("name") or self.name
        if snap.get("price"):
            px = float(snap["price"])
            chg = float(snap.get("change_pct") or 0)
            self.quote_lbl.setText(
                f"{px:.2f}  {chg:+.2f}%  {snap.get('quote_time') or ''}")
            self.quote_lbl.setStyleSheet(
                f"color:{pct_color(chg)}; font-size:15px; font-weight:600;")
        parts = []
        if snap.get("price"):
            parts.append(
                f"<b>{nm} {self.code}</b> 现价 {snap['price']:.2f}"
                f"（{snap.get('change_pct', 0):+.2f}%）")
            parts.append(
                f"今开 {snap.get('open', 0):.2f} · 昨收 "
                f"{snap.get('prev_close', 0):.2f} · 最高 "
                f"{snap.get('high', 0):.2f} · 最低 {snap.get('low', 0):.2f}")
        elif nm:
            parts.append(f"<b>{nm} {self.code}</b>")
        if events:
            buys = [e for e in events if "买入" in e[1]]
            sells = [e for e in events if "卖出" in e[1]]
            if buys:
                e = buys[-1]
                parts.append(f"最近买入：{e[0]}（{e[1]} {e[2]:+.2f} 分）")
            if sells:
                e = sells[-1]
                parts.append(f"最近卖出：{e[0]}（{e[1]} {e[2]:+.2f} 分）")
        if data.get("err"):
            parts.append("<span style='color:#B45309'>"
                         + "；".join(data["err"]) + "</span>")
        parts.append("信号为日线技术面回溯（未含市场环境修正）")
        self.info.setText(" · ".join(parts))

        self._draw_minute(minute, snap)
        self._draw_daily(daily, events)
        try:
            self.gw.ci.layout.setRowStretchFactor(0, 3)
            self.gw.ci.layout.setRowStretchFactor(1, 1)
            self.gw.ci.layout.setRowStretchFactor(2, 2)
        except Exception:
            pass

    def closeEvent(self, event):
        for w in self._workers:
            try:
                w.done.disconnect()
                w.error.disconnect()
                w.finished.disconnect()
            except Exception:
                pass
        super().closeEvent(event)

    def _draw_minute(self, minute, snap):
        if minute is None or len(minute) < 2:
            return
        dates = list(dict.fromkeys(minute["date"].tolist()))
        keep = dates[-2:]                     # 最近两个交易日
        m = minute[minute["date"].isin(keep)].reset_index(drop=True)
        if len(m) < 2:
            return
        x = np.arange(len(m))
        labels = m["time"].tolist()
        closes = m["close"].astype(float).to_numpy()
        opens = m["open"].astype(float).to_numpy()
        vols = m["volume"].astype(float).to_numpy()

        p1 = self.gw.addPlot(row=0, col=0,
                             axisItems={"bottom": _TickAxis(labels)})
        p1.plot(x, closes, pen=pg.mkPen("#2E6FD6", width=1.6))
        p1.showGrid(x=True, y=True, alpha=0.2)
        p1.setLabel("left", "价格")

        # 当日快照叠加：现价 / 昨收 水平线，并在日界处画分隔线
        pc = float(snap.get("price") or closes[-1])
        self._price_line = pg.InfiniteLine(
            pos=pc, angle=0, pen=pg.mkPen(UP, width=1.2),
            label=f"现价 {pc:.2f}",
            labelOpts={"position": 0.05, "color": UP})
        p1.addItem(self._price_line)
        prev = float(snap.get("prev_close") or 0)
        if prev > 0:
            p1.addItem(pg.InfiniteLine(
                pos=prev, angle=0,
                pen=pg.mkPen("#8A8F99", style=Qt.DashLine),
                label=f"昨收 {prev:.2f}",
                labelOpts={"position": 0.05, "color": "#8A8F99"}))
        if len(keep) == 2:
            edge = int((m["date"] == keep[-1]).to_numpy().argmax())
            if edge > 0:
                p1.addItem(pg.InfiniteLine(
                    pos=edge - 0.5, angle=90,
                    pen=pg.mkPen("#C9CDD4", style=Qt.DashLine)))
            ymax = float(np.nanmax(closes)) if len(closes) else 1.0
            for d, idx in ((keep[0], 1), (keep[-1], edge + 1)):
                t = pg.TextItem(d[5:], color=TEXT2, anchor=(0, 0))
                t.setPos(idx, ymax * 1.01)
                p1.addItem(t)

        p2 = self.gw.addPlot(row=1, col=0)
        p2.setXLink(p1)
        p2.addItem(pg.BarGraphItem(
            x=x, height=vols, width=0.7,
            brushes=[UP if c >= o else DOWN for c, o in zip(closes, opens)]))
        p2.setLabel("left", "成交量")
        p2.getAxis("bottom").setStyle(showValues=False)

    def _draw_daily(self, daily, events):
        if daily is None or len(daily) < 30:
            return
        d = daily.tail(90)
        dates = [i.strftime("%m-%d") for i in d.index]
        x = np.arange(len(d))
        closes = d["close"].astype(float).to_numpy()

        p3 = self.gw.addPlot(row=2, col=0,
                             axisItems={"bottom": _TickAxis(dates)})
        p3.plot(x, closes, pen=pg.mkPen("#2E6FD6", width=1.2))
        p3.showGrid(x=True, y=True, alpha=0.2)
        p3.setLabel("left", "收盘")

        pos = {i.strftime("%Y-%m-%d"): k for k, i in enumerate(d.index)}
        bx, by, sx, sy = [], [], [], []
        for date, grade, _score in events:
            k = pos.get(date)
            if k is None:
                continue
            (bx if "买入" in grade else sx).append(k)
            (by if "买入" in grade else sy).append(closes[k])
        if bx:
            p3.addItem(pg.ScatterPlotItem(
                bx, by, symbol="t1", size=11, brush=UP, pen=pg.mkPen(UP),
                name="买入信号"))
        if sx:
            p3.addItem(pg.ScatterPlotItem(
                sx, sy, symbol="t", size=11, brush=DOWN, pen=pg.mkPen(DOWN),
                name="卖出信号"))
        if bx or sx:
            p3.addLegend(offset=(4, 4))

        # 标注最近一次信号
        if events:
            date, grade, score = events[-1]
            k = pos.get(date)
            if k is not None:
                color = UP if "买入" in grade else DOWN
                t = pg.TextItem(f"{date[5:]} {grade} {score:+.1f}",
                                color=color, anchor=(0, 1))
                t.setPos(k, closes[k])
                p3.addItem(t)


class WatchlistPage(QWidget):
    def __init__(self, set_status: Callable[[str], None]):
        super().__init__()
        self.set_status = set_status
        self.items: List[dict] = []
        self.signals: List[dict] = []
        self._workers = []
        self._dialogs = []
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

    @staticmethod
    def _in_trading_session() -> bool:
        """是否处于 A 股连续竞价时段（周一至周五 9:30-11:30、13:00-15:00）。"""
        now = dt.datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.hour * 100 + now.minute
        return (930 <= t <= 1130) or (1300 <= t <= 1500)

    def _tick_auto(self):
        """盘中定时刷新：仅交易时段、且当前没有在算时才触发。"""
        if not self.auto_cb.isChecked() or not self._in_trading_session():
            return
        if any(w.isRunning() for w in self._workers):
            return
        self.refresh_signals(silent=True)

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
                self._render_env(self.signals[0].get("market"))
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
        self.intraday_btn = QPushButton("分时图")
        self.intraday_btn.setToolTip("查看选中个股的 5 分钟分时、当日快照与买卖信号标注")
        self.intraday_btn.clicked.connect(self.open_intraday)
        self.refresh_btn = QPushButton("刷新买卖信号")
        self.refresh_btn.setObjectName("Primary")
        self.refresh_btn.clicked.connect(self.refresh_signals)
        self.auto_cb = QCheckBox("盘中自动刷新")
        self.auto_cb.setChecked(True)
        self.auto_cb.setToolTip(
            "交易时段内每 60 秒自动拉取最新价与信号（收盘/非交易时段不刷新）")
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
        bl.addWidget(self.auto_cb)
        bl.addWidget(self.intraday_btn)
        bl.addWidget(self.refresh_btn)
        root.addWidget(bar)

        self._timer = QTimer(self)
        self._timer.setInterval(60000)
        self._timer.timeout.connect(self._tick_auto)
        self._timer.start()

        # 市场环境 + 信号规则说明（信号计算方式透明化）
        self.env_lbl = QLabel(
            "市场环境加载中…（信号 = 技术面多空加权 + 大盘/情绪修正；"
            "均线±4.5、MACD±2、RSI±1.5、布林/量能/动量各±1）")
        self.env_lbl.setObjectName("Hint")
        self.env_lbl.setWordWrap(True)
        root.addWidget(self.env_lbl)

        split = QSplitter(Qt.Vertical)
        self.tbl = make_table(
            ["代码", "名称", "现价 / 涨幅%", "信号", "置信度", "多空分",
             "RSI", "量比", "近5日%", "近20日%"], stretch_last=False)
        self.tbl.setColumnWidth(0, 70)
        self.tbl.setColumnWidth(1, 100)
        self.tbl.setColumnWidth(3, 90)
        # 列数少且内容短：平均铺满卡片宽度，避免表格右侧出现大片空白
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.itemSelectionChanged.connect(self._show_detail)
        self.tbl.cellDoubleClicked.connect(lambda _r, _c: self.open_intraday())
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

    # ---------------- 实时行情（全局被动刷新，3 秒） ----------------
    def live_codes(self):
        return [str(x.get("code")) for x in self.items if x.get("code")]

    def apply_live(self, quotes: dict):
        """只更新现价与涨幅，不重算指标，避免盘中频繁计算。"""
        if not self.items or self.tbl.rowCount() == 0:
            return
        for row in range(self.tbl.rowCount()):
            it = self.tbl.item(row, 0)
            if it is None:
                continue
            code = it.text()
            q = (quotes or {}).get(code)
            if not q or not q.get("price"):
                continue
            px = float(q["price"])
            chg = float(q.get("change_pct") or 0)
            cellp = self.tbl.item(row, 2)
            if cellp is not None:
                cellp.setText(f"{px:.2f}  {chg:+.2f}%")
                cellp.setForeground(QColor(pct_color(chg) if chg else TEXT))
            for s in self.signals:
                if str(s.get("code")) == code:
                    s["price"] = px
                    s["change_pct"] = chg
                    s["quote_note"] = "盘中实时"
                    break
        if self.tbl.currentRow() >= 0:
            self._show_detail()

    def open_intraday(self):
        """打开选中个股的分时窗口（也可双击表格行）。"""
        row = None
        rows = self.tbl.selectionModel().selectedRows()
        if rows:
            row = rows[0].row()
        elif self.tbl.currentRow() >= 0:
            row = self.tbl.currentRow()
        if row is None:
            self.set_status("请先选择一个自选股，或双击表格行")
            return
        item = self.tbl.item(row, 0)
        if item is None:
            return
        st = item.data(Qt.UserRole) or {}
        code = st.get("code") if isinstance(st, dict) else None
        code = code or item.text()
        if not (len(code) == 6 and code.isdigit()):
            return
        name_item = self.tbl.item(row, 1)
        name = name_item.text() if name_item else ""
        dlg = IntradayDialog(code, name, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dialogs = [d for d in self._dialogs if d.isVisible()]
        self._dialogs.append(dlg)
        dlg.show()

    def refresh_signals(self, silent: bool = False):
        if not self.items:
            self.tbl.setRowCount(0)
            self.detail.clear()
            return
        self._silent = bool(silent)
        if not silent:
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
            self._render_env(sigs[0].get("market"))
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
            chg = float(s.get("change_pct") or 0)
            self.tbl.setItem(r, 2, cell(
                f"{s['price']:.2f}  {chg:+.2f}%",
                pct_color(chg) if chg else TEXT,
                align=Qt.AlignCenter, sort_value=s["price"]))
            self.tbl.item(r, 2).setToolTip(
                f"现价 {s['price']:.2f}，当日 {chg:+.2f}%"
                + (f"（{s.get('quote_note')}）" if s.get("quote_note") else ""))
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
        if not getattr(self, "_silent", False):
            self.set_status(f"自选信号已刷新：{len(sigs)} 只")
            self.tbl.selectRow(0)

    def _render_env(self, env):
        """自选页顶部的市场环境与信号规则说明。"""
        rule = ("信号 = 日线技术面加权（均线±4.5、MACD±2、RSI±1.5、"
                "布林/量能/动量各±1）；收盘后形成、次日执行；"
                "盘中价格与涨幅按实时快照更新（默认每 60 秒）")
        if not env:
            self.env_lbl.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
            self.env_lbl.setText("市场环境未获取：信号仅按技术面计算。" + rule)
            return
        bias = signal_engine.env_bias(env)
        color = UP if bias >= 0.3 else (DOWN if bias <= -0.3 else TEXT2)
        self.env_lbl.setStyleSheet(f"color:{color}; font-size:12px;")
        self.env_lbl.setText(
            f"市场环境：{env.get('note', '')} → 对个股总分修正 "
            f"{bias:+.2f} 分（偏多加分 / 偏弱减分）。{rule}")

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
        raw = float(s.get("score_raw", s["score"]))
        bias = float(s.get("env_bias", 0.0) or 0.0)
        html = f"<b style='font-size:14px;color:{_SIG_COLOR.get(sig, TEXT)}'>"
        html += f"{s['code']} {s['name']} → {sig}</b>"
        html += (f"（置信度 {s['confidence']:.0%}，总分 {s['score']:+.1f}"
                 f" = 技术分 {raw:+.1f} + 环境修正 {bias:+.2f}）<br>")
        chg = float(s.get("change_pct") or 0)
        note = s.get("quote_note") or "收盘价"
        html += (f"现价 {s['price']:.2f}，当日 "
                 f"<span style='color:{pct_color(chg)}'>{chg:+.2f}%</span>"
                 f"（{note}）<br>")
        env = s.get("market") or {}
        if env:
            html += (f"<span style='color:{TEXT2}'>市场环境："
                     f"{env.get('note', '')}</span><br>")
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
        html += ("<br><br><span style='color:" + TEXT2 + "; font-size:11px'>"
                 "信号规则：均线多头/金叉/MA20 上方各加分（权重最高），MACD 状态与"
                 "红绿柱、RSI 超买超卖、布林触轨、量比与 5/20 日动量共同打分；"
                 "|总分|≥4 为强烈、≥1.5 为买入/卖出，市场环境按指数与情绪温度"
                 "再修正约 ±2 分。</span>")
        self.detail.setHtml(html)
