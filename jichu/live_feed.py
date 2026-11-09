"""全局行情推送（被动刷新）。

各页面只声明自己关心的股票代码（live_codes）并提供 apply_live(quotes)，
主窗口统一注册后由本模块每 3 秒拉取一次最新报价并广播，
页面无需各自写定时器，也不会重复请求同一只股票。
"""

import datetime as dt
import threading
from typing import Callable, Iterable, List

from PySide6.QtCore import QObject, QTimer, Signal

import data_sources


class LiveFeed(QObject):
    """定时批量拉取订阅代码的实时行情，并以信号广播给全部页面。"""

    updated = Signal(dict, str)       # {code: quote}, 时间字符串

    def __init__(self, interval_ms: int = 3000, parent=None):
        super().__init__(parent)
        self.interval_ms = interval_ms
        self._providers: List[Callable[[], Iterable[str]]] = []
        self._busy = False
        self._timer: QTimer | None = None
        self.last_ts = ""

    # ---------- 注册 ----------
    def register(self, provider: Callable[[], Iterable[str]]):
        if provider not in self._providers:
            self._providers.append(provider)

    def start(self):
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(self.interval_ms)

    # ---------- 交易时段 ----------
    @staticmethod
    def in_session() -> bool:
        now = dt.datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.hour * 100 + now.minute
        return (930 <= t <= 1130) or (1300 <= t <= 1500)

    # ---------- 轮询 ----------
    def tick(self):
        if self._busy or not self.in_session():
            return
        codes: List[str] = []
        for p in list(self._providers):
            try:
                codes.extend(x for x in (p() or []) if x)
            except Exception:
                continue
        codes = [c for c in dict.fromkeys(codes)
                 if len(str(c)) == 6 and str(c).isdigit()][:400]
        if not codes:
            return
        self._busy = True
        threading.Thread(target=self._work, args=(codes,), daemon=True).start()

    def _work(self, codes: List[str]):
        quotes = {}
        try:
            quotes = data_sources.fetch_quotes_batch(codes)
        except Exception:
            quotes = {}
        finally:
            self._busy = False
        if quotes:
            self.last_ts = dt.datetime.now().strftime("%H:%M:%S")
            self.updated.emit(quotes, self.last_ts)
