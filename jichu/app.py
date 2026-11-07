# -*- coding: utf-8 -*-
"""
量化选股系统 · 主窗口
====================
本次界面优化：
  · 启动自动按屏幕可用区域估算尺寸并居中（不再固定 1440×900 超出小屏，
    保证最大化/最小化/关闭按钮始终可见，无需拖动窗口）；
  · 高 DPI 缩放策略统一，避免缩放后布局溢出；
  · 六个功能页统一 16px 外边距、12px 分组间距，页面切换带淡入过渡；
  · 启动先渲染本地快照（秒开），再后台并发刷新各区块（流式填充）；
  · 顶部栏展示数据时间与状态，底部状态栏展示抓取进度。

入口：启动量化系统.bat → .venv\Scripts\python.exe app.py
"""

import datetime as dt
import sys

from PySide6.QtCore import QTimer, Qt, QSize
from PySide6.QtGui import QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QPushButton, QVBoxLayout, QWidget,
                               QButtonGroup, QStatusBar)

from pages_backtest import BacktestPage
from pages_info import MarketInfoPage
from pages_picker import PickerPage
from pages_recommend import RecommendPage
from pages_strategy import StrategyPage
from pages_watch import WatchlistPage
from ui_common import (BG, CARD, TEXT, TEXT2, TOPBAR, ACCENT, global_qss,
                       fit_window_to_screen, FadeStackedWidget, UP, DOWN,
                       WARN)
import market_info

BATCH_TIMES = {"morning": (9, 27), "noon": (12, 57)}


class NavButton(QPushButton):
    """左侧导航按钮：编号 + 名称 + 英文副标题。"""
    def __init__(self, index: int, title: str, subtitle: str):
        super().__init__()
        self.setObjectName("Nav")
        self.setCheckable(True)
        self.setFixedHeight(48)
        self.setText(f"  {index}   {title}\n        {subtitle}")
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
        QPushButton#Nav {{
            color: #C7CCD4; background: transparent; border: none;
            border-left: 3px solid transparent; text-align: left;
            padding: 4px 14px; font-size: 13px;
        }}
        QPushButton#Nav:hover {{ background: rgba(255,255,255,0.07);
            color: #FFFFFF; }}
        QPushButton#Nav:checked {{
            background: rgba(200,22,29,0.14); color: #FFFFFF;
            border-left: 3px solid {ACCENT}; font-weight: bold;
        }}
        """)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("量化选股系统 · 市场信息 / 形态选股 / 多因子荐股 / 策略学习")
        # 按屏幕可用区域自适应并居中（关键：解决标题栏按钮出屏）
        fit_window_to_screen(self, prefer=(1440, 900), ratio=(0.92, 0.88),
                             min_size=(1080, 680))
        self.setStyleSheet(global_qss())
        self._snapshot_refresh_guard = 0.0
        self._auto_gen = {}
        self._build()
        self._setup_timers()
        self._show_cached_snapshot()
        # 窗口显示后再开始占网络的刷新，避免拖慢首帧
        QTimer.singleShot(120, self._startup_refresh)
        QTimer.singleShot(300, self._center_check)

    # ---------------- 布局 ----------------
    def _build(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左侧导航
        nav = QFrame()
        nav.setFixedWidth(210)
        nav.setStyleSheet(f"background: #1B2330; border-right: 1px solid #2A3342;")
        nl = QVBoxLayout(nav)
        nl.setContentsMargins(0, 0, 0, 12)
        nl.setSpacing(2)

        brand = QFrame()
        brand.setFixedHeight(88)
        bt = QVBoxLayout(brand)
        bt.setContentsMargins(18, 18, 12, 8)
        t1 = QLabel("量化选股系统")
        t1.setStyleSheet("color:#FFFFFF; font-size:17px; font-weight:bold;")
        t2 = QLabel("A-SHARE QUANT TERMINAL")
        t2.setStyleSheet("color:#7C8798; font-size:10px; letter-spacing:1px;")
        bt.addWidget(t1)
        bt.addWidget(t2)
        nl.addWidget(brand)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_btns = []
        items = [
            ("1", "市场信息", "MARKET NEWS"),
            ("2", "自选信号", "WATCHLIST"),
            ("3", "策略形态选股", "PATTERN SCREENER"),
            ("4", "开盘前荐股", "MULTI-FACTOR PICKS"),
            ("5", "策略学习", "STRATEGY LIBRARY"),
            ("6", "量化回测", "BACKTEST"),
        ]
        for i, (idx, title, sub) in enumerate(items):
            b = NavButton(idx, title, sub)
            b.clicked.connect(lambda checked=False, x=i: self._switch_page(x))
            self.nav_group.addButton(b, i)
            self.nav_btns.append(b)
            nl.addWidget(b)
        nl.addStretch()

        foot = QLabel("数据源：东财 · 同花顺 · 财联社 · 新浪 · 腾讯\n"
                      "红涨绿跌 · 仅供研究，不构成投资建议")
        foot.setStyleSheet("color:#6C7686; font-size:10px; padding: 10px 16px;")
        foot.setWordWrap(True)
        nl.addWidget(foot)
        root.addWidget(nav)

        # 右侧主区
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        top = QFrame()
        top.setFixedHeight(62)
        top.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #1E2634, stop:1 #2C3A52);")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(18, 10, 18, 10)
        tl.setSpacing(12)
        title = QLabel("市场概览")
        title.setObjectName("H1")
        self.page_title = title
        self.market_lbl = QLabel("正在加载市场数据…")
        self.market_lbl.setStyleSheet("color:#C7CCD4; font-size:12px;")
        self.src_lbl = QLabel("")
        self.src_lbl.setObjectName("Pill")
        self.btn_refresh = QPushButton("刷新信息")
        self.btn_refresh.setObjectName("Ghost")
        self.btn_refresh.clicked.connect(self._refresh_info)
        self.btn_pick = QPushButton("立即生成荐股")
        self.btn_pick.setObjectName("Ghost")
        self.btn_pick.clicked.connect(self._generate_picks)
        tl.addWidget(title)
        tl.addSpacing(10)
        tl.addWidget(self.market_lbl)
        tl.addWidget(self.src_lbl)
        tl.addStretch()
        tl.addWidget(self.btn_refresh)
        tl.addWidget(self.btn_pick)
        rl.addWidget(top)

        self.stack = FadeStackedWidget()
        self.info_page = MarketInfoPage(self.set_status)
        self.watch_page = WatchlistPage(self.set_status)
        self.picker_page = PickerPage(self.set_status)
        self.recommend_page = RecommendPage(
            self.set_status, lambda: self.info_page._snap,
            self._accept_snapshot)
        self.strategy_page = StrategyPage(
            self.set_status, lambda: self.info_page._snap)
        self.backtest_page = BacktestPage(self.set_status)
        for page in (self.info_page, self.watch_page, self.picker_page,
                     self.recommend_page, self.strategy_page,
                     self.backtest_page):
            self.stack.addWidget(page)
        rl.addWidget(self.stack, stretch=1)
        root.addWidget(right, stretch=1)
        self.setCentralWidget(central)

        # 状态栏
        sb = QStatusBar()
        sb.setSizeGripEnabled(True)
        self.status_lbl = QLabel("准备就绪")
        self.clock_lbl = QLabel("")
        self.clock_lbl.setStyleSheet("color:#9AA4B2;")
        sb.addWidget(self.status_lbl, 1)
        sb.addPermanentWidget(self.clock_lbl)
        self.setStatusBar(sb)

        # 页面联动
        self.info_page.snapshot_loaded.connect(self._accept_snapshot)
        self._switch_page(0, first=True)

    # ---------------- 页面切换 ----------------
    def _switch_page(self, index: int, first: bool = False):
        if index < 0 or index >= self.stack.count():
            return
        self.nav_btns[index].setChecked(True)
        titles = ["市场信息", "自选信号", "策略形态选股",
                  "开盘前多因子荐股", "机构策略学习", "量化回测"]
        self.page_title.setText(titles[index])
        if index == 2:
            self.set_status("设置市值/板块/形态条件后点击“开始选股”")
        elif index == 3:
            self.set_status("开盘前 T-3 分钟自动生成，也可手动生成多因子名单")
        elif index == 4:
            self.set_status("左侧选择机构策略，先学习公开方法论，再套用近似权重选股")
        elif index == 5:
            self.set_status("输入代码与均线参数，运行回测查看净值/回撤/仓位")
        if first:
            self.stack.setCurrentIndex(index)
        else:
            self.stack.fade_to(index)

    def _center_check(self):
        """兜底：若窗口因任务栏/DPI 变化部分出屏，重新校正一次。"""
        scr = self.screen() or QApplication.primaryScreen()
        if not scr:
            return
        avail = scr.availableGeometry()
        g = self.frameGeometry()
        if g.right() > avail.right() + 4 or g.bottom() > avail.bottom() + 4 \
                or g.left() < avail.left() - 4 or g.top() < avail.top() - 4:
            fit_window_to_screen(self)

    # ---------------- 定时器 ----------------
    def _setup_timers(self):
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)
        self._tick()

        # 开盘前自动生成检查：每 30 秒
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._check_auto_generate)
        self.auto_timer.start(30_000)
        QTimer.singleShot(5_000, self._check_auto_generate)

    def _tick(self):
        self.clock_lbl.setText(
            dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def _check_auto_generate(self):
        now = dt.datetime.now()
        if now.weekday() >= 5:
            return
        for slot, (h, m) in BATCH_TIMES.items():
            if now.hour == h and m <= now.minute <= m + 2:
                key = f"{slot}_{now.date()}"
                if self._auto_gen.get(slot) == str(now.date()):
                    continue
                self._auto_gen[slot] = str(now.date())
                self.set_status(
                    f"到达 {h:02d}:{m:02d}，自动生成{dict([('morning', '早盘'), ('noon', '午盘')])[slot]}批荐股…")
                self.recommend_page.auto_generate(slot)

    # ---------------- 启动与刷新 ----------------
    def _show_cached_snapshot(self):
        snap = market_info.load_latest_snapshot()
        if snap is None:
            return
        try:
            self.info_page.render_snapshot(snap)
            age = ""
            if getattr(snap, "asof", ""):
                age = f" · 快照 {snap.asof}"
            self.set_status("已展示本地快照，正在后台刷新最新数据…" + age)
        except Exception:
            pass

    def _startup_refresh(self):
        self.info_page.refresh(force=False)
        self.watch_page.start_auto_refresh()

    def _refresh_info(self):
        self._switch_page(0)
        self.info_page.refresh(force=True)

    def _generate_picks(self):
        self._switch_page(3)
        self.recommend_page.generate()

    def _accept_snapshot(self, snap):
        """信息页快照更新：同步顶部栏，并错峰刷新自选信号。"""
        try:
            s = snap.sentiment or {}
            self.market_lbl.setText(
                f"交易日 {snap.trade_date} · 涨停 {s.get('zt_count', 0)} · "
                f"跌停 {s.get('dt_count', 0)} · 情绪 {s.get('temperature', 0)}"
                f"（{s.get('label', '—')}）")
            srcs = getattr(snap, "news_sources", []) or []
            tag = "本地缓存" if getattr(snap, "from_cache", False) else "实时"
            self.src_lbl.setText(
                f"{tag} · 新闻源 {len(srcs)} 家")
            color = UP if s.get("temperature", 0) >= 50 else (
                DOWN if s.get("temperature", 0) < 35 else WARN)
            self.market_lbl.setStyleSheet(
                f"color:{color}; font-size:12px;")
        except Exception:
            pass
        # 自选信号在信息刷新完成 2 秒后错峰计算，避免同时抢网络
        QTimer.singleShot(2000, self.watch_page.start_auto_refresh)

    # ---------------- 状态 ----------------
    def set_status(self, msg: str):
        self.status_lbl.setText(msg)

    # ---------------- 关闭 ----------------
    def closeEvent(self, event):
        for t in (getattr(self, "clock", None),
                  getattr(self, "auto_timer", None)):
            if t:
                t.stop()
        for page in (self.info_page, self.watch_page, self.picker_page,
                     self.recommend_page, self.strategy_page,
                     self.backtest_page):
            for w in list(getattr(page, "_workers", [])):
                try:
                    w.cancel()
                    if not w.wait(2500):
                        # 仍有阻塞中的网络请求时强制结束，避免窗口关闭后
                        # QThread 对象先于线程销毁导致进程崩溃
                        w.terminate()
                        w.wait(500)
                except Exception:
                    pass
        event.accept()


def main():
    # Qt6 默认已启用高 DPI；这里显式设置缩放取整策略，避免 125%/150% 下
    # 字体与控件尺寸跳变、布局溢出。
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("量化选股系统")
    app.setFont(QFont("Microsoft YaHei", 9))
    win = MainWindow()
    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
