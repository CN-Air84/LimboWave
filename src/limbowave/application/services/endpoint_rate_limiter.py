"""应用级站点限流：线程/异步客户端共用一个单调时钟、平滑发送。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from threading import Event, Lock

from limbowave.domain.providers import EndpointConfig


class RateLimitCancelled(Exception):
    """应用关闭或调用取消；尚未发送的请求不能继续出站。"""


class EndpointRateLimiter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._last: dict[str, float] = {}
        self._rpm: dict[str, int] = {}
        self._stopped = Event()

    def configure(self, endpoints: Iterable[EndpointConfig]) -> None:
        """更新额度但保留已发送记录，保存设置不会获得额外突发额度。"""
        with self._lock:
            self._rpm = {endpoint.id: endpoint.rpm for endpoint in endpoints}

    def try_acquire(self, endpoint_id: str, rpm: int = 5) -> float:
        """返回 0 表示已占用本次额度，否则返回剩余等待秒数（不预占未来额度）。"""
        with self._lock:
            if self._stopped.is_set():
                raise RateLimitCancelled("站点请求已停止")
            limit = self._rpm.get(endpoint_id, rpm)
            if limit < 1:
                raise ValueError("RPM 必须为正整数")
            now = self._clock()
            last = self._last.get(endpoint_id)
            delay = 0.0 if last is None else max(0.0, last + 60.0 / limit - now)
            if delay == 0:
                self._last[endpoint_id] = now
            return delay

    def wait(self, endpoint: EndpointConfig, *, cancelled: Event | None = None) -> None:
        """供同步 HTTP 探测线程使用；取消的等待不占用将来额度。"""
        while True:
            if cancelled is not None and cancelled.is_set():
                raise RateLimitCancelled("检测已取消")
            delay = self.try_acquire(endpoint.id, endpoint.rpm)
            if delay == 0:
                return
            self._stopped.wait(min(delay, 0.1))

    def stop(self) -> None:
        """唤醒所有后台等待，避免退出时继续排队请求。"""
        with self._lock:
            self._stopped.set()


# 无应用装配的直接调用也默认限流；GUI 装配自己的实例以控制生命周期。
DEFAULT_RATE_LIMITER = EndpointRateLimiter()
