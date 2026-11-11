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
BG = "#F3F5F9"          # 窗口背景
CARD = "#FFFFFF"
TOPBAR = "#FFFFFF"      # 顶部/导航浅色（亮色主题）
NAV_BG = "#FFFFFF"
ACCENT = "#C8161D"      # 同花顺红
ACCENT_LIGHT = "#E5403B"
TEXT = "#202634"
TEXT2 = "#66707F"       # 次级文字：加深，保证浅色背景上可读
BORDER = "#E5E9F1"
ROW_ALT = "#F8FAFD"
SELECT = "#FDEDED"
HEAD_BG = "#F6F8FC"     # 表头浅底
HEAD_TEXT = "#5A6473"
INFO = "#2E6FD6"
WARN = "#D98A09"

# 渐变（QSS 用）
BG_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
           "stop:0 #F5F7FA, stop:1 #E8EDF4)")
TOPBAR_GRAD = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, "
               "stop:0 #FFFFFF, stop:1 #F4F7FB)")
NAV_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #FFFFFF, stop:1 #F5F8FC)")
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

    /* ---- 卡片与标题 ---- */
    #TopBar {{ background: {TOPBAR_GRAD}; }}
    QFrame#Card {{ background: {CARD}; border: 1px solid {BORDER};
        border-radius: 12px; }}
    QFrame#Toolbar {{ background: {CARD}; border: 1px solid {BORDER};
        border-radius: 12px; }}
    QFrame#Divider {{ background: {BORDER}; max-height: 1px; border: none; }}
    QLabel#CardTitle {{ color: {TEXT}; font-size: 14px; font-weight: bold;
        letter-spacing: 0.3px; padding-bottom: 2px; }}
    QLabel#SectionTitle {{ color: {TEXT}; font-size: 15px; font-weight: bold; }}
    QLabel#Hint {{ color: {TEXT2}; font-size: 12px; }}
    QLabel#Pill {{ background: #EEF2F8; color: #5A6473; border-radius: 10px;
        padding: 3px 10px; font-size: 12px; }}
    QLabel#H1 {{ color: {TEXT}; font-size: 16px; font-weight: bold;
        letter-spacing: 0.5px; }}
    QLabel#H2 {{ color: {TEXT2}; font-size: 11px; }}

    /* ---- 按钮 ---- */
    QPushButton {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 8px; padding: 6px 15px; min-height: 18px;
        color: {TEXT};
    }}
    QPushButton:hover {{ border-color: #F0BEBC; color: {ACCENT};
        background: #FFFAFA; }}
    QPushButton:pressed {{ background: #F2F5F9; }}
    QPushButton:disabled {{ color: #AEB6C2; background: #F5F7FA;
        border-color: #EDF0F5; }}
    QPushButton#Primary {{
        background: {PRIMARY_GRAD}; color: #FFFFFF; border: none;
        border-radius: 8px; padding: 7px 18px; font-weight: bold;
    }}
    QPushButton#Primary:hover {{ background: {PRIMARY_HOVER_GRAD};
        color: #FFFFFF; }}
    QPushButton#Primary:pressed {{ background: #A90E13; }}
    QPushButton#Primary:disabled {{ background: #E3E7EF; color: #AEB6C2; }}
    QPushButton#Danger {{
        background: #FFF3F2; color: {ACCENT}; border: 1px solid #F3C6C4;
    }}
    QPushButton#Danger:hover {{ background: #FDE8E7; border-color: #EAB3B0; }}
    QPushButton#GhostBtn {{
        background: #FFF6F6; color: {ACCENT}; border: 1px solid #F5D2D0;
        border-radius: 8px; padding: 6px 14px;
    }}
    QPushButton#GhostBtn:hover {{ background: #FFEAE9;
        border-color: #EAB3B0; }}
    QPushButton#Ghost {{
        background: #F5F7FA; color: #4A5568;
        border: 1px solid #E2E8F0; border-radius: 8px;
        padding: 6px 14px;
    }}
    QPushButton#Ghost:hover {{ background: #EAEFF6;
        color: {ACCENT}; border-color: #D5DDE8; }}
    QPushButton#Tab {{ text-align: left; padding: 8px 14px;
        border-radius: 8px; }}
    QPushButton#Tab:checked {{
        background: #FFF1F0; color: {ACCENT}; border: 1px solid #F3C6C4;
        font-weight: bold;
    }}

    /* ---- 输入 ---- */
    QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 8px; padding: 6px 10px; min-height: 22px;
        selection-background-color: #FBDDDC; selection-color: {TEXT};
    }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover,
    QDoubleSpinBox:hover {{ border-color: #C9D0DC; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
    QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; background: #FFFCFC; }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
        color: #AEB6C2; background: #F5F7FA; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{
        image: none; border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {TEXT2}; margin-right: 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 8px; padding: 4px;
        selection-background-color: {SELECT};
        selection-color: {TEXT}; outline: none;
    }}

    /* ---- 勾选 ---- */
    QCheckBox {{ spacing: 6px; color: {TEXT2}; font-size: 12px; }}
    QCheckBox::indicator {{ width: 15px; height: 15px;
        border: 1px solid #C9D0DC; border-radius: 4px; background: #FFFFFF; }}
    QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT};
        border-color: {ACCENT}; }}
    QCheckBox:disabled {{ color: #AEB6C2; }}

    /* ---- 表格 ---- */
    QTableWidget {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 10px; gridline-color: transparent;
        selection-background-color: {SELECT};
        selection-color: {TEXT};
        alternate-background-color: {ROW_ALT};
    }}
    QTableWidget::item {{ padding: 6px 8px; border-bottom: 1px solid #F1F4F8; }}
    QTableWidget::item:selected {{ background: {SELECT}; color: {TEXT}; }}
    QHeaderView::section {{
        background: {HEAD_BG}; color: {HEAD_TEXT};
        border: none; border-bottom: 1px solid #E3E8F1;
        border-right: 1px solid #EFF3F9;
        padding: 9px 8px; font-weight: bold; font-size: 12px;
    }}
    QTableCornerButton::section {{ background: {HEAD_BG}; border: none; }}

    /* ---- 页签 ---- */
    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px;
        background: {CARD}; top: -1px; }}
    QTabBar::tab {{ background: #F2F5FA; color: {TEXT2};
        border: 1px solid transparent; border-top-left-radius: 8px;
        border-top-right-radius: 8px; padding: 7px 16px; margin-right: 3px;
        font-size: 12px; }}
    QTabBar::tab:selected {{ background: {CARD}; color: {ACCENT};
        border: 1px solid {BORDER}; border-bottom-color: {CARD};
        font-weight: bold; }}
    QTabBar::tab:hover:!selected {{ background: #E9EEF6; color: {TEXT}; }}

    /* ---- 文本区 ---- */
    QTextBrowser, QTextEdit {{
        background: {CARD}; border: 1px solid {BORDER};
        border-radius: 10px; padding: 10px;
        selection-background-color: #FBDDDC;
    }}

    /* ---- 进度条 ---- */
    QProgressBar {{
        background: #EDF1F7; border: none; border-radius: 5px;
        min-height: 9px; max-height: 9px; text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 #E54540, stop:1 #F08A86);
        border-radius: 5px;
    }}

    /* ---- 滚动条 ---- */
    QScrollBar:vertical {{ background: transparent; width: 9px;
        margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #D3D9E3; border-radius: 4px;
        min-height: 32px; }}
    QScrollBar::handle:vertical:hover {{ background: #B7C0CE; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 9px;
        margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #D3D9E3; border-radius: 4px;
        min-width: 32px; }}
    QScrollBar::handle:horizontal:hover {{ background: #B7C0CE; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    /* ---- 状态栏 ---- */
    QStatusBar {{
        background: #FFFFFF; color: #4A5568; font-size: 12px;
        border-top: 1px solid #E5E9F1;
    }}
    QStatusBar::item {{ border: none; }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:hover {{ background: #E9EDF3; }}
    QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px;
        margin-top: 10px; padding-top: 6px; font-weight: bold; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px;
        padding: 0 4px; color: {TEXT2}; }}
    QToolTip {{ background: #2B313B; color: #FFFFFF; border: none;
        padding: 6px 8px; font-size: 12px; border-radius: 6px; }}
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
    h.setFixedHeight(36)
    h.setDefaultAlignment(Qt.AlignCenter)
    if stretch_last:
        h.setStretchLastSection(True)
    else:
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
    t.verticalHeader().setDefaultSectionSize(34)
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
