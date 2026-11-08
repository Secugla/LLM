# -*- coding: utf-8 -*-
"""信息抓取页面：情绪温度、大盘指数、涨停池、热点概念、行业板块、财经新闻。

本次优化：
  · 界面先秒开：表格先用骨架占位，后台各数据块完成即分区渲染（流式）；
  · 顶部显示数据源与进度条，抓取过程可见、不空白；
  · 涨停池/行业/新闻均为可滚动表格且表头固定，行高与字号统一；
  · 实时抓取失败时保留上次缓存内容并在顶部标注，不阻塞其他区块。
"""
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFrame, QSplitter, QComboBox,
                               QDialog, QProgressBar)

from ui_common import (UP, DOWN, FLAT, TEXT2, CAT_COLOR, card,
                       StreamWorker, make_table, cell, pct_color, fmt_amount,
                       skeleton_table)
import market_info


class IndexCard(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.setMinimumWidth(118)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(13, 9, 13, 10)
        lay.setSpacing(3)
        self.name = QLabel("—")
        self.name.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.price = QLabel("—")
        self.price.setStyleSheet("font-size:19px; font-weight:bold;")
        self.pct = QLabel("—")
        self.pct.setStyleSheet("font-size:12px;")
        lay.addWidget(self.name)
        lay.addWidget(self.price)
        lay.addWidget(self.pct)

    def set_data(self, name, price, pct):
        self.name.setText(name)
        self.price.setText(f"{price:,.2f}")
        c = pct_color(pct)
        self.price.setStyleSheet(
            f"font-size:19px; font-weight:bold; color:{c};")
        self.pct.setText(f"{pct:+.2f}%")
        self.pct.setStyleSheet(f"font-size:12px; color:{c};")


class MarketInfoPage(QWidget):
    snapshot_loaded = Signal(object)

    def __init__(self, set_status: Callable[[str], None]):
        super().__init__()
        self.set_status = set_status
        self._snap = None
        self._workers = []
        self._busy = False
        self._build()
        self._show_skeleton()

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # 顶部：刷新 + 情绪温度 + 数据源 + 进度（共两行，长文本不挤压单行）
        top = QFrame()
        top.setObjectName("Card")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(16, 11, 16, 11)
        tl.setSpacing(4)
        r1 = QHBoxLayout()
        r1.setSpacing(14)
        self.btn = QPushButton("刷新最新消息")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(lambda: self.refresh(force=True))
        self.temp_lbl = QLabel("情绪温度：—")
        self.temp_lbl.setStyleSheet("font-size:16px; font-weight:bold;")
        self.breadth_lbl = QLabel("等待数据…")
        self.breadth_lbl.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.asof_lbl = QLabel("")
        self.asof_lbl.setStyleSheet(f"color:{TEXT2}; font-size:12px;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(150)
        self.progress.setVisible(False)
        r1.addWidget(self.btn)
        r1.addWidget(self.temp_lbl)
        r1.addWidget(self.breadth_lbl)
        r1.addStretch()
        r1.addWidget(self.progress)
        tl.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addStretch()
        r2.addWidget(self.asof_lbl)
        tl.addLayout(r2)
        root.addWidget(top)

        # 指数卡片行
        self.index_cards = {}
        row = QHBoxLayout()
        row.setSpacing(12)
        for nm in ["上证指数", "深证成指", "创业板指", "科创50", "北证50"]:
            c = IndexCard()
            self.index_cards[nm] = c
            row.addWidget(c)
        root.addLayout(row)

        # 中部：涨停池 + 右侧（热点概念/行业板块）
        mid = QSplitter(Qt.Horizontal)
        mid.setChildrenCollapsible(False)
        self.zt_tbl = make_table(
            ["代码", "名称", "涨幅%", "连板", "封板资金", "首次封板",
             "炸板", "所属行业", "涨停原因"])
        self.zt_tbl.setColumnWidth(0, 68)
        self.zt_tbl.setColumnWidth(1, 92)
        self.zt_tbl.setColumnWidth(3, 48)
        self.zt_tbl.setColumnWidth(7, 96)
        mid.addWidget(card("今日涨停池 · 涨停原因", self.zt_tbl))

        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        self.hot_tbl = make_table(["热点概念", "涨停数", "代表股"])
        self.hot_tbl.setColumnWidth(0, 118)
        self.hot_tbl.setColumnWidth(1, 60)
        self.sec_tbl = make_table(["行业板块", "涨幅%", "领涨股"])
        self.sec_tbl.setColumnWidth(0, 118)
        right.addWidget(card("热点概念 · 涨停聚类", self.hot_tbl))
        right.addWidget(card("行业板块 · 轮动", self.sec_tbl))
        right.setSizes([320, 300])
        mid.addWidget(right)
        mid.setSizes([700, 340])
        root.addWidget(mid, stretch=3)

        # 资金面：龙虎榜 + 北向活跃股
        fund = QSplitter(Qt.Horizontal)
        fund.setChildrenCollapsible(False)
        self.lhb_tbl = make_table(
            ["代码", "名称", "涨幅%", "净买额", "成交额", "上榜原因"])
        self.lhb_tbl.setColumnWidth(0, 68)
        self.lhb_tbl.setColumnWidth(1, 92)
        self.lhb_tbl.setColumnWidth(2, 62)
        self.lhb_tbl.setColumnWidth(3, 92)
        self.lhb_tbl.setColumnWidth(4, 86)
        self.lhb_tbl.setColumnWidth(5, 210)
        self.lhb_tbl.setToolTip("双击个股：查看当日买入/卖出前五席位、游资别名与历史胜率")
        self.lhb_tbl.setMinimumHeight(220)
        self.lhb_tbl.cellDoubleClicked.connect(self._open_seats)
        fund.addWidget(card("龙虎榜 · 净买入榜", self.lhb_tbl))

        nb_card = QFrame()
        nb_card.setObjectName("Card")
        nbl = QVBoxLayout(nb_card)
        nbl.setContentsMargins(14, 12, 14, 14)
        nbl.setSpacing(8)
        cap2 = QLabel("北向资金 · 十大活跃股（按成交额）")
        cap2.setObjectName("CardTitle")
        self.nb_note = QLabel("净买额自 2024-08-19 起停止披露")
        self.nb_note.setObjectName("Hint")
        self.nb_note.setWordWrap(True)
        nbl.addWidget(cap2)
        nbl.addWidget(self.nb_note)
        self.nb_tbl = make_table(["市场", "代码", "名称", "涨幅%", "成交额"],
                                 stretch_last=False)
        self.nb_tbl.setColumnWidth(0, 66)
        self.nb_tbl.setColumnWidth(1, 68)
        self.nb_tbl.setColumnWidth(2, 92)
        self.nb_tbl.setColumnWidth(3, 62)
        self.nb_tbl.setColumnWidth(4, 90)
        self.nb_tbl.setMinimumHeight(220)
        nbl.addWidget(self.nb_tbl)
        fund.addWidget(nb_card)
        fund.setSizes([720, 480])
        root.addWidget(fund, stretch=2)

        # 底部：新闻
        news_card = QFrame()
        news_card.setObjectName("Card")
        nl = QVBoxLayout(news_card)
        nl.setContentsMargins(14, 11, 14, 13)
        nl.setSpacing(8)
        nbar = QHBoxLayout()
        cap = QLabel("财经新闻 · 多源聚合")
        cap.setObjectName("CardTitle")
        self.news_src_lbl = QLabel("")
        self.news_src_lbl.setObjectName("Hint")
        self.cat_box = QComboBox()
        self.cat_box.addItem("全部分类")
        for c in CAT_COLOR:
            self.cat_box.addItem(c)
        self.cat_box.currentTextChanged.connect(self._fill_news)
        nbar.addWidget(cap)
        nbar.addSpacing(10)
        nbar.addWidget(self.news_src_lbl)
        nbar.addStretch()
        nbar.addWidget(QLabel("筛选："))
        nbar.addWidget(self.cat_box)
        nl.addLayout(nbar)
        self.news_tbl = make_table(["时间", "分类", "情绪", "标题"])
        self.news_tbl.setColumnWidth(0, 175)
        self.news_tbl.setColumnWidth(1, 82)
        self.news_tbl.setColumnWidth(2, 56)
        nl.addWidget(self.news_tbl)
        root.addWidget(news_card, stretch=2)

    def _show_skeleton(self):
        skeleton_table(self.zt_tbl, 10, "正在获取涨停池…")
        skeleton_table(self.hot_tbl, 7, "正在聚类热点…")
        skeleton_table(self.sec_tbl, 7, "正在获取板块…")
        skeleton_table(self.lhb_tbl, 6, "正在获取龙虎榜…")
        skeleton_table(self.nb_tbl, 6, "正在获取北向活跃股…")
        skeleton_table(self.news_tbl, 8, "正在聚合财经快讯…")

    # ---------------- 数据 ----------------
    def refresh(self, force: bool = False):
        if self._busy:
            return
        self._busy = True
        self.btn.setEnabled(False)
        self.progress.setVisible(True)
        self.set_status("正在并发抓取最新市场信息…")
        w = StreamWorker(lambda p, e: market_info.refresh_all_stream(
            progress=p, emit=e, force=force))
        w.progress.connect(self.set_status)
        w.partial.connect(self._on_partial)
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

    def render_snapshot(self, snap):
        """公开渲染入口（缓存快照或实时快照均可）。"""
        self._on_done(snap)

    def _on_error(self, msg):
        self.set_status("信息抓取失败：" + msg)
        self.asof_lbl.setText("抓取失败，保留上次数据")
        self._on_finished()

    def _on_partial(self, stage: str, payload: dict):
        """流式渲染：每个数据块完成即更新对应区域。"""
        if stage == "indices":
            self._render_indices(payload.get("indices") or {})
        elif stage == "limit_up":
            self._snap_trade_date = payload.get("trade_date", "")
            self._render_zt(payload.get("limit_up") or [])
        elif stage == "sectors":
            if payload.get("sectors"):
                self._render_sectors(payload["sectors"])
        elif stage == "boards":
            self._render_boards(payload)
        elif stage == "news":
            self._render_news(payload)
        elif stage == "lhb":
            self._render_lhb(payload.get("lhb") or [])
        elif stage == "sentiment":
            self._render_sentiment(payload.get("sentiment") or {})
        elif stage == "north":
            self._render_northbound(payload.get("northbound") or {})
        elif stage == "hot":
            self._render_hot(payload.get("hot_concepts") or [])
            if payload.get("limit_up"):
                self._render_zt(payload["limit_up"])
            self._render_sentiment(payload.get("sentiment") or {})

    def _render_indices(self, indices: dict):
        for nm, c in self.index_cards.items():
            v = indices.get(nm)
            if v:
                c.set_data(nm, v["price"], v["change_pct"])

    def _render_sentiment(self, s: dict):
        if not s:
            return
        temp_v = s.get("temperature", 0)
        self.temp_lbl.setText(f"情绪温度：{temp_v}  {s.get('label', '—')}")
        tc = UP if temp_v >= 50 else (DOWN if temp_v < 35 else FLAT)
        self.temp_lbl.setStyleSheet(
            f"font-size:16px; font-weight:bold; color:{tc};")
        self.breadth_lbl.setText(
            f"涨停 {s.get('zt_count', 0)}  跌停 {s.get('dt_count', 0)}  "
            f"炸板率 {s.get('zha_ban_rate', 0)}%  "
            f"连板高度 {s.get('max_boards', 0)}  连板股 {s.get('lianban', 0)}")

    def _render_zt(self, rows: list):
        self.zt_tbl.setSortingEnabled(False)
        self.zt_tbl.setRowCount(0)
        for s_ in rows:
            r = self.zt_tbl.rowCount()
            self.zt_tbl.insertRow(r)
            self.zt_tbl.setItem(r, 0, cell(s_["code"], align=Qt.AlignCenter))
            self.zt_tbl.setItem(r, 1, cell(s_["name"], bold=True))
            self.zt_tbl.setItem(r, 2, cell(
                f"{s_['change_pct']:.2f}", pct_color(s_["change_pct"]),
                align=Qt.AlignCenter))
            self.zt_tbl.setItem(r, 3, cell(
                s_["boards"], UP, bold=True, align=Qt.AlignCenter,
                sort_value=s_.get("boards", 0)))
            self.zt_tbl.setItem(r, 4, cell(
                fmt_amount(s_["seal_fund"]), align=Qt.AlignCenter,
                sort_value=s_.get("seal_fund", 0)))
            self.zt_tbl.setItem(r, 5, cell(s_["first_seal"],
                                           align=Qt.AlignCenter))
            self.zt_tbl.setItem(r, 6, cell(s_["break_count"],
                                           align=Qt.AlignCenter,
                                           sort_value=s_.get("break_count", 0)))
            self.zt_tbl.setItem(r, 7, cell(s_["industry"]))
            c9 = cell("、".join(s_.get("reasons", [])))
            if s_.get("reason_detail"):
                c9.setToolTip(s_["reason_detail"])
            self.zt_tbl.setItem(r, 8, c9)

    def _render_hot(self, hot: list):
        self.hot_tbl.setRowCount(0)
        for h in hot:
            r = self.hot_tbl.rowCount()
            self.hot_tbl.insertRow(r)
            self.hot_tbl.setItem(r, 0, cell(h["concept"], bold=True))
            self.hot_tbl.setItem(r, 1, cell(
                h["count"], UP, bold=True, align=Qt.AlignCenter,
                sort_value=h["count"]))
            self.hot_tbl.setItem(r, 2, cell("、".join(h["stocks"])))

    def _render_sectors(self, sectors: list):
        self.sec_tbl.setRowCount(0)
        for x in sectors:
            r = self.sec_tbl.rowCount()
            self.sec_tbl.insertRow(r)
            self.sec_tbl.setItem(r, 0, cell(x.get("name", "")))
            self.sec_tbl.setItem(r, 1, cell(
                f"{x.get('change_pct', 0):.2f}",
                pct_color(x.get("change_pct", 0)), align=Qt.AlignCenter,
                sort_value=x.get("change_pct", 0)))
            self.sec_tbl.setItem(r, 2, cell(x.get("lead", "")))

    def _render_boards(self, payload: dict):
        """东财行业板块（更细、带领涨股）；无数据时保留新浪行业。"""
        boards = payload.get("em_industry_boards") or []
        if boards:
            self.sec_tbl.setRowCount(0)
            for x in boards[:40]:
                r = self.sec_tbl.rowCount()
                self.sec_tbl.insertRow(r)
                self.sec_tbl.setItem(r, 0, cell(x.get("name", "")))
                self.sec_tbl.setItem(r, 1, cell(
                    f"{x.get('change_pct', 0):.2f}",
                    pct_color(x.get("change_pct", 0)), align=Qt.AlignCenter,
                    sort_value=x.get("change_pct", 0)))
                self.sec_tbl.setItem(r, 2, cell(x.get("lead", "")))
            self.sec_tbl.setSortingEnabled(True)

    def _render_lhb(self, rows: list):
        self._lhb_date = rows[0].get("date", "") if rows else ""
        t = self.lhb_tbl
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for x in rows:
            r = t.rowCount()
            t.insertRow(r)
            net = float(x.get("net_buy", 0) or 0)
            t.setItem(r, 0, cell(x.get("code", ""), align=Qt.AlignCenter))
            t.setItem(r, 1, cell(x.get("name", ""), bold=True))
            t.setItem(r, 2, cell(
                f"{x.get('change_pct', 0):+.2f}",
                pct_color(x.get("change_pct", 0)), align=Qt.AlignCenter,
                sort_value=x.get("change_pct", 0)))
            t.setItem(r, 3, cell(
                fmt_amount(net), UP if net >= 0 else DOWN, bold=True,
                align=Qt.AlignCenter, sort_value=net))
            t.setItem(r, 4, cell(
                fmt_amount(x.get("amount", 0)), align=Qt.AlignCenter,
                sort_value=x.get("amount", 0)))
            c5 = cell("、".join(x.get("reasons", [])[:2]))
            if x.get("turnover"):
                c5.setToolTip(f"换手率 {x['turnover']:.2f}%")
            t.setItem(r, 5, c5)
        t.horizontalHeader().setSortIndicator(3, Qt.DescendingOrder)
        t.setSortingEnabled(True)

    def _open_seats(self, row: int, _col: int):
        """双击龙虎榜个股，查看当日买卖前五席位与该席位历史表现。"""
        code = self.lhb_tbl.item(row, 0).text()
        name = self.lhb_tbl.item(row, 1).text()
        date = getattr(self, "_lhb_date", "") or ""
        self.set_status(f"读取 {code} 席位明细…")
        try:
            seats = market_info.fetch_lhb_seats(date) if date else {}
        except Exception:
            seats = {}
        slot = seats.get(code) or {}
        alias = market_info.load_seat_alias()

        dlg = QDialog(self)
        dlg.setWindowTitle(f"席位明细 · {code} {name}")
        dlg.resize(1120, 560)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QLabel(f"{code} {name}｜{date}｜席位质量 {slot.get('quality', 0):+.2f}")
        head.setObjectName("CardTitle")
        lay.addWidget(head)
        tip = QLabel("3日历史胜率 = 该席位历史上榜后 3 日上涨概率（东财口径）；"
                     "别名为本地可编辑映射（.cache/seat_alias.json），仅供参考")
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        box = QHBoxLayout()
        for side, title in (("buys", "买入前 5 席位"),
                            ("sells", "卖出前 5 席位")):
            t = make_table(["席位 / 别名", "金额", "3日历史胜率", "样本"])
            t.setColumnWidth(0, 520)
            t.setColumnWidth(1, 110)
            t.setColumnWidth(2, 110)
            t.setColumnWidth(3, 70)
            for x in slot.get(side) or []:
                r = t.rowCount()
                t.insertRow(r)
                label = market_info.seat_alias_of(x["seat"], alias) or ""
                txt = x["seat"] + (f"（{label}）" if label else "")
                c0 = cell(txt)
                c0.setToolTip(txt)
                t.setItem(r, 0, c0)
                t.setItem(r, 1, cell(fmt_amount(x["amount"]),
                                     align=Qt.AlignCenter,
                                     sort_value=x["amount"]))
                prob = x.get("prob3")
                t.setItem(r, 2, cell(
                    f"{float(prob):.1f}%" if prob is not None else "—",
                    pct_color((float(prob) - 50) if prob is not None else 0),
                    align=Qt.AlignCenter))
                t.setItem(r, 3, cell(str(x.get("samples") or "—"),
                                     align=Qt.AlignCenter))
            box.addWidget(card(title, t))
        lay.addLayout(box)
        self.set_status("")
        dlg.exec()

    def _render_northbound(self, payload: dict):
        rows = payload.get("rows") or []
        note = payload.get("note") or ""
        d = payload.get("date") or ""
        mk = payload.get("market") or {}
        parts = [f"数据日 {d}"] if d else []
        if mk.get("total"):
            parts.append(f"沪股通成交 {fmt_amount(mk.get('sh', 0))}")
            parts.append(f"深股通成交 {fmt_amount(mk.get('sz', 0))}")
            parts.append(f"北向合计成交 {fmt_amount(mk.get('total', 0))}")
        parts.append(note or "净买额自 2024-08-19 起停止披露")
        self.nb_note.setText("｜".join(parts))
        t = self.nb_tbl
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for x in rows:
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, cell(x.get("market", ""), TEXT2,
                                 align=Qt.AlignCenter))
            t.setItem(r, 1, cell(x.get("code", ""), align=Qt.AlignCenter))
            t.setItem(r, 2, cell(x.get("name", ""), bold=True))
            t.setItem(r, 3, cell(
                f"{x.get('change_pct', 0):+.2f}",
                pct_color(x.get("change_pct", 0)), align=Qt.AlignCenter,
                sort_value=x.get("change_pct", 0)))
            amt = float(x.get("deal_amt", 0) or 0)
            t.setItem(r, 4, cell(
                fmt_amount(amt), align=Qt.AlignCenter, sort_value=amt))
        t.horizontalHeader().setSortIndicator(4, Qt.DescendingOrder)
        t.setSortingEnabled(True)

    def _render_news(self, payload: dict):
        srcs = payload.get("news_sources") or []
        self.news_src_lbl.setText(
            f"来源：{'、'.join(srcs)}" if srcs else "")
        # 临时保存，供分类筛选
        self._news_cache = payload.get("news") or []
        self._fill_news()

    # ---------------- 完整渲染 ----------------
    _SNAP_FIELDS = ("indices", "limit_up", "sectors", "em_industry_boards",
                    "em_concept_boards", "news", "news_sources", "lhb",
                    "northbound", "hot_concepts", "rotation", "sentiment")

    def _merge_snapshot(self, new, old):
        """新快照优先覆盖旧快照；仅当某数据源本次未返回时才沿用上次，避免界面变空。"""
        if old is None or getattr(new, "from_cache", False):
            return new, []
        reused = []
        for f in self._SNAP_FIELDS:
            if not getattr(new, f, None) and getattr(old, f, None):
                setattr(new, f, getattr(old, f))
                reused.append(f)
        if reused:
            msg = "部分数据源未返回，已沿用上次数据：" + "、".join(reused)
            new.errors = list(getattr(new, "errors", []) or []) + [msg]
        return new, reused

    def _on_done(self, snap):
        snap, self._reused = self._merge_snapshot(snap, self._snap)
        self._snap = snap
        self._news_cache = snap.news
        if not getattr(snap, "from_cache", False):
            try:
                market_info.save_latest_snapshot(snap)
            except Exception:
                pass

        self._render_indices(snap.indices or {})
        self._render_sentiment(snap.sentiment or {})
        self._render_zt(snap.limit_up or [])
        self._render_hot(snap.hot_concepts or [])
        if snap.em_industry_boards:
            self._render_boards({"em_industry_boards":
                                 snap.em_industry_boards})
        else:
            self._render_sectors(snap.sectors or [])
        self._render_news({"news": snap.news or [],
                           "news_sources": snap.news_sources or []})
        lhb_rows = getattr(snap, "lhb", None) or []
        if lhb_rows:
            self._render_lhb(lhb_rows)
        else:
            skeleton_table(self.lhb_tbl, 6, "点击右上角刷新，获取当日龙虎榜")
        nb = getattr(snap, "northbound", None) or {}
        if nb.get("rows"):
            self._render_northbound(nb)
        else:
            skeleton_table(self.nb_tbl, 6, "点击右上角刷新，获取北向活跃股")
        self.zt_tbl.setSortingEnabled(True)
        self.hot_tbl.setSortingEnabled(True)
        self.sec_tbl.setSortingEnabled(True)

        src_tag = "【本地缓存】" if getattr(snap, "from_cache", False) \
            else "【实时数据】"
        if getattr(self, "_reused", None):
            src_tag += "（部分区块沿用上次）"
        srcs = getattr(snap, "news_sources", []) or []
        src_txt = f" · 新闻源 {len(srcs)} 家" if srcs else ""
        self.asof_lbl.setText(
            f"{src_tag} 交易日 {snap.trade_date} · 更新于 "
            f"{getattr(snap, 'asof', '')}{src_txt}")

        s = snap.sentiment or {}
        self.set_status(
            f"信息刷新完成：涨停{s.get('zt_count', 0)} "
            f"跌停{s.get('dt_count', 0)} 新闻{len(snap.news or [])}条")
        self.snapshot_loaded.emit(snap)

    def _fill_news(self):
        news = getattr(self, "_news_cache", None)
        if news is None and self._snap:
            news = self._snap.news
        if not news:
            return
        filt = self.cat_box.currentText()
        self.news_tbl.setRowCount(0)
        for n in news:
            if filt != "全部分类" and n.get("category") != filt:
                continue
            r = self.news_tbl.rowCount()
            self.news_tbl.insertRow(r)
            self.news_tbl.setItem(r, 0, cell(n.get("time", ""), TEXT2,
                                             align=Qt.AlignCenter))
            cat = n.get("category", "其他")
            self.news_tbl.setItem(r, 1, cell(
                cat, CAT_COLOR.get(cat, FLAT), bold=True,
                align=Qt.AlignCenter))
            sent = n.get("sentiment", 0)
            sc = UP if sent > 0.15 else (DOWN if sent < -0.15 else FLAT)
            mark = "利好" if sent > 0.15 else ("利空" if sent < -0.15 else "中性")
            self.news_tbl.setItem(r, 2, cell(mark, sc, align=Qt.AlignCenter))
            c3 = cell(n.get("title", ""))
            tip = " · ".join(x for x in [n.get("source", ""),
                                         n.get("summary", "")] if x)
            if tip:
                c3.setToolTip(tip)
            self.news_tbl.setItem(r, 3, c3)
