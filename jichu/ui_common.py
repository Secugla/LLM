# -*- coding: utf-8 -*-
"""
UI 共享组件
===========
配色（同花顺浅色风格，红涨绿跌）、全局样式表（渐变背景/渐变按钮/圆角卡片）、
后台 Worker 线程（普通/流式）、表格工具（可排序单元格、骨架占位、导出 CSV）、
窗口尺寸自适应与页面淡入切换。

本次优化新增：
  · fit_window_to_screen：按屏幕可用分辨率估算窗口尺寸并居中，避免标题栏按钮出屏
  · FadeStackedWidget：页面切换淡入过渡
  · StreamWorker：支持「边抓取边渲染」的流式后台任务
  · skeleton_table / clear_table：加载中的占位行，避免空白卡死
  · SortableItem：表格按数值/文本正确排序
"""

import csv
import threading
from typing import Callable, List, Optional

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, QThread, Qt,
                            Signal)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect,
                               QHeaderView, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QLabel, QWidget)

# ---------- 配色 ----------
UP = "#E0332E"          # 涨（红）
DOWN = "#1BA784"        # 跌（绿）
FLAT = "#8A9099"
BG = "#F2F3F5"          # 窗口背景
CARD = "#FFFFFF"
TOPBAR = "#222B3A"      # 顶部/导航深色
NAV_BG = "#1F2733"
ACCENT = "#C8161D"      # 同花顺红
ACCENT_LIGHT = "#E5403B"
TEXT = "#262A30"
TEXT2 = "#8A9099"
BORDER = "#E4E7EC"
ROW_ALT = "#F7F9FC"
SELECT = "#FDF0F0"
HEAD_BG = "#2E3949"
INFO = "#2E6FD6"
WARN = "#D98A09"

# 渐变（QSS 用）
BG_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
           "stop:0 #F5F7FA, stop:1 #E8EDF4)")
TOPBAR_GRAD = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, "
               "stop:0 #1E2634, stop:1 #2C3A52)")
NAV_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #222B3A, stop:1 #191F2B)")
PRIMARY_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                "stop:0 #E54540, stop:1 #C01218)")
PRIMARY_HOVER_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                      "stop:0 #F05853, stop:1 #D62A30)")
BTN_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #FFFFFF, stop:1 #F1F3F6)")

# 新闻分类配色
CAT_COLOR = {
    "政策政治": "#D98A09",
    "宏观经济": "#2E6FD6",
    "行业产业": "#7A4FD0",
    "公司公告": "#3D9A63",
    "国际地缘": "#C0504D",
    "网络舆情": "#D66BA0",
    "其他": "#8A9099",
}

FONT = "Microsoft YaHei"


def global_qss() -> str:
    return f"""
    QWidget {{ font-family: "{FONT}"; color: {TEXT}; font-size: 13px; }}
    QMainWindow, QDialog {{ background: {BG_GRAD}; }}
    #TopBar {{ background: {TOPBAR_GRAD}; }}
    QFrame#Card {{ background: {CARD}; border: 1px solid {BORDER};
        border-radius: 10px; }}
    QFrame#Toolbar {{ background: {CARD}; border: 1px solid {BORDER};
        border-radius: 10px; }}
    QFrame#Divider {{ background: {BORDER}; max-height: 1px; border: none; }}
    QLabel#CardTitle {{ color: {TEXT}; font-size: 14px; font-weight: bold;
        padding-bottom: 2px; }}
    QLabel#SectionTitle {{ color: {TEXT}; font-size: 15px; font-weight: bold; }}
    QLabel#Hint {{ color: {TEXT2}; font-size: 12px; }}
    QLabel#Pill {{ background: #F1F4F9; color: #5D6573; border-radius: 9px;
        padding: 3px 10px; font-size: 12px; }}
    QLabel#H1 {{ color: #FFFFFF; font-size: 16px; font-weight: bold; }}
    QLabel#H2 {{ color: #C7CCD4; font-size: 12px; }}

    /* ---- 按钮 ---- */
    QPushButton {{
        background: {BTN_GRAD}; border: 1px solid {BORDER};
        border-radius: 7px; padding: 7px 16px;
        color: {TEXT};
    }}
    QPushButton:hover {{ border-color: #C9CDD4; color: {ACCENT}; }}
    QPushButton:pressed {{ background: #E6E9EE; }}
    QPushButton:disabled {{ color: #A9ADB4; background: #F0F1F4;
        border-color: {BORDER}; }}
    QPushButton#Primary {{
        background: {PRIMARY_GRAD}; color: #FFFFFF; border: none;
        padding: 8px 20px; font-weight: bold;
    }}
    QPushButton#Primary:hover {{ background: {PRIMARY_HOVER_GRAD}; color: #FFFFFF; }}
    QPushButton#Primary:pressed {{ background: #A90E13; }}
    QPushButton#Danger {{
        background: #FFF3F2; color: {ACCENT}; border: 1px solid #F3C6C4;
    }}
    QPushButton#Danger:hover {{ background: #FDE8E7; }}
    QPushButton#Ghost {{
        background: rgba(255,255,255,0.08); color: #C7CCD4;
        border: 1px solid rgba(255,255,255,0.18); border-radius: 6px;
    }}
    QPushButton#Ghost:hover {{ background: rgba(255,255,255,0.16);
        color: #FFFFFF; }}
    QPushButton#Tab {{ text-align: left; padding: 8px 14px; }}
    QPushButton#Tab:checked {{
        background: #FFF1F0; color: {ACCENT}; border: 1px solid #F3C6C4;
        font-weight: bold;
    }}

    /* ---- 输入 ---- */
    QLineEdit, QComboBox, QDateEdit, QSpinBox {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 6px; padding: 6px 9px; min-height: 20px;
    }}
    QLineEdit:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{
        image: none; border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {TEXT2}; margin-right: 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 6px; selection-background-color: {SELECT};
        selection-color: {TEXT}; outline: none;
    }}

    /* ---- 表格 ---- */
    QTableWidget {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 8px; gridline-color: transparent;
        selection-background-color: {SELECT};
        selection-color: {TEXT};
        alternate-background-color: {ROW_ALT};
    }}
    QTableWidget::item {{ padding: 5px 6px; border-bottom: 1px solid #F0F2F5; }}
    QHeaderView::section {{
        background: {HEAD_BG}; color: #E8EBF0;
        border: none; border-right: 1px solid #3A4658;
        padding: 9px 8px; font-weight: bold; font-size: 12px;
    }}
    QTableCornerButton::section {{ background: {HEAD_BG}; border: none; }}

    /* ---- 文本区 ---- */
    QTextBrowser, QTextEdit {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 8px; padding: 8px;
    }}

    /* ---- 进度条 ---- */
    QProgressBar {{
        background: #E9EDF3; border: none; border-radius: 4px;
        height: 8px; text-align: center; color: transparent;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 #E54540, stop:1 #F08A86);
        border-radius: 4px;
    }}

    /* ---- 滚动条 ---- */
    QScrollBar:vertical {{ background: transparent; width: 10px;
        margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #C9CDD4; border-radius: 4px;
        min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: #A9ADB4; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px;
        margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #C9CDD4; border-radius: 4px;
        min-width: 30px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    /* ---- 状态栏 ---- */
    QStatusBar {{
        background: {TOPBAR_GRAD}; color: #C7CCD4; font-size: 12px;
        border-top: 1px solid #2C3A52;
    }}
    QStatusBar::item {{ border: none; }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:hover {{ background: #E9EDF3; }}
    QToolTip {{ background: #2B313B; color: #FFFFFF; border: none;
        padding: 6px; font-size: 12px; }}
    """


# ---------- 后台 Worker ----------
class Worker(QThread):
    """普通后台任务：task(progress) → 返回值通过 done 信号送回主线程。"""
    progress = Signal(str)
    done = Signal(object)
    error = Signal(str)

    def __init__(self, task: Callable[[Callable[[str], None]], object]):
        super().__init__()
        self._task = task
        self._cancel = threading.Event()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def cancel(self):
        self._cancel.set()

    def run(self):
        try:
            res = self._task(self.progress.emit)
            self.done.emit(res)
        except Exception as e:  # noqa
            self.error.emit(f"{type(e).__name__}: {e}")


class StreamWorker(QThread):
    """
    流式后台任务：task(progress, emit) 。
    emit(stage:str, payload:object) 会把阶段性结果实时送回主线程，
    实现「界面先出来、各区块各自填充」。
    """
    progress = Signal(str)
    partial = Signal(str, object)
    done = Signal(object)
    error = Signal(str)

    def __init__(self, task: Callable[[Callable[[str], None],
                                       Callable[[str, object], None]], object]):
        super().__init__()
        self._task = task
        self._cancel = threading.Event()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def cancel(self):
        self._cancel.set()

    def run(self):
        try:
            res = self._task(self.progress.emit, self.partial.emit)
            self.done.emit(res)
        except Exception as e:  # noqa
            self.error.emit(f"{type(e).__name__}: {e}")


# ---------- 表格工具 ----------
class SortableItem(QTableWidgetItem):
    """支持按数值/文本正确排序的单元格。

    注意：不要回退到 super().__lt__，Qt 的排序回调与 Python 重载叠加时
    可能反复回调本方法导致递归溢出，这里统一给出确定的比较结果。
    """
    def __init__(self, text: str, sort_value=None):
        super().__init__(str(text))
        self._sort_value = sort_value

    def __lt__(self, other):
        if not isinstance(other, QTableWidgetItem):
            return NotImplemented
        a = self._sort_value
        b = getattr(other, "_sort_value", None)
        if a is not None and b is not None:
            try:
                if type(a) is not type(b):
                    a, b = float(a), float(b)
                return a < b
            except Exception:
                pass
        return self.text() < other.text()


def make_table(headers: List[str], aligns: Optional[List[int]] = None,
               stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setSelectionMode(QTableWidget.ExtendedSelection)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setVerticalScrollMode(QTableWidget.ScrollPerPixel)
    t.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
    h = t.horizontalHeader()
    h.setHighlightSections(False)
    h.setFixedHeight(34)
    h.setDefaultAlignment(Qt.AlignCenter)
    if stretch_last:
        h.setStretchLastSection(True)
    else:
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
    t.verticalHeader().setDefaultSectionSize(32)
    return t


def cell(text, color: Optional[str] = None, bold: bool = False,
         align: int = Qt.AlignLeft | Qt.AlignVCenter,
         sort_value=None):
    """创建表格单元格；sort_value 传入数值可按数值排序。"""
    it = SortableItem(str(text), sort_value)
    it.setTextAlignment(align)
    if color:
        it.setForeground(QBrush(QColor(color)))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    return it


def clear_table(table: QTableWidget):
    table.setRowCount(0)


def skeleton_table(table: QTableWidget, rows: int = 8, text: str = "加载中…"):
    """加载骨架：用浅灰占位行填充表格，避免整块空白。"""
    cols = table.columnCount()
    table.setRowCount(0)
    for r in range(rows):
        table.insertRow(r)
        for c in range(cols):
            label = text if c == 1 else "·" * (2 + r % 3)
            it = cell(label, "#B9C0CC", align=Qt.AlignCenter)
            table.setItem(r, c, it)
    table.setSortingEnabled(False)


def export_table_csv(table: QTableWidget, path: str) -> int:
    """把表格内容导出为 UTF-8 BOM CSV（Excel 直接打开不乱码）。"""
    headers = [table.horizontalHeaderItem(c).text()
               for c in range(table.columnCount())]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in range(table.rowCount()):
            w.writerow([(table.item(r, c).text() if table.item(r, c) else "")
                        for c in range(table.columnCount())])
    return table.rowCount()


def pct_color(v) -> str:
    try:
        v = float(v)
    except Exception:
        return FLAT
    if v > 0:
        return UP
    if v < 0:
        return DOWN
    return FLAT


def fmt_amount(v, unit: str = "元") -> str:
    """金额格式化：元 → 亿/万。"""
    try:
        v = float(v)
    except Exception:
        return str(v)
    if unit == "元":
        if abs(v) >= 1e8:
            return f"{v / 1e8:.2f}亿"
        if abs(v) >= 1e4:
            return f"{v / 1e4:.0f}万"
        return f"{v:,.0f}"
    if unit == "亿":
        return f"{v:.2f}亿"
    if unit == "万":
        return f"{v:.0f}万"
    return str(v)


# ---------- 视觉组件 ----------
def card(title: str = "", inner: Optional[QWidget] = None,
         spacing: int = 8, margins=(14, 12, 14, 14)) -> QFrame:
    """统一风格的卡片容器。"""
    box = QFrame()
    box.setObjectName("Card")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    if title:
        t = QLabel(title)
        t.setObjectName("CardTitle")
        lay.addWidget(t)
    if inner is not None:
        lay.addWidget(inner, stretch=1)
    return box


def divider() -> QFrame:
    d = QFrame()
    d.setObjectName("Divider")
    d.setFixedHeight(1)
    return d


def fit_window_to_screen(win, prefer=(1440, 900),
                         ratio=(0.92, 0.88), min_size=(1080, 680)) -> None:
    """
    按当前屏幕可用区域估算窗口尺寸并居中。
    解决：固定 1440×900 在 1366×768 / 高缩放屏幕上超出桌面，
    导致标题栏的最大化/关闭按钮看不见、需要拖动窗口才出现。
    """
    scr = win.screen() or QApplication.primaryScreen()
    if scr is None:
        win.resize(*prefer)
        return
    avail = scr.availableGeometry()
    w = min(prefer[0], max(min_size[0], int(avail.width() * ratio[0])))
    h = min(prefer[1], max(min_size[1], int(avail.height() * ratio[1])))
    # 小屏时进一步收缩，确保完整落入可用区域（含标题栏与状态栏）
    w = min(w, avail.width() - 16)
    h = min(h, avail.height() - 40)
    win.setMinimumSize(min(min_size[0], avail.width() - 16),
                       min(min_size[1], avail.height() - 40))
    win.resize(w, h)
    win.move(avail.x() + (avail.width() - w) // 2,
             avail.y() + (avail.height() - h) // 2)


def center_window(win) -> None:
    """仅居中，不改变尺寸。"""
    scr = win.screen() or QApplication.primaryScreen()
    if scr is None:
        return
    avail = scr.availableGeometry()
    g = win.frameGeometry()
    g.moveCenter(avail.center())
    win.move(g.topLeft())


class FadeStackedWidget(QStackedWidget):
    """页面切换淡入过渡，避免区块切换突兀。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._anim = None

    def fade_to(self, index: int, duration: int = 220):
        if index < 0 or index >= self.count():
            return
        if index == self.currentIndex():
            return
        w = self.widget(index)
        effect = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(effect)
        self.setCurrentIndex(index)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(duration)
        anim.setStartValue(0.15)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(lambda: w.setGraphicsEffect(None))
        self._anim = anim
        anim.start()
