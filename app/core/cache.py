"""
两种容器：
- TtlLruCache：键值对，LRU + 每条 TTL，写入超 max_size 淘汰最老
- SlidingWindowCounter：每键一个时间戳 deque，用于滑动窗口频控

通用 janitor：所有 cache 注册到 _registry，统一周期清理。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import Any, Generic, Optional, TypeVar


K = TypeVar("K")
V = TypeVar("V")


class _Cleanable:
    name: str = "cache"

    async def cleanup(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def size(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError


_registry: list[_Cleanable] = []


def register(cache: _Cleanable) -> None:
    _registry.append(cache)


class TtlLruCache(_Cleanable, Generic[K, V]):
    """LRU + 每条 TTL。set 超容时淘汰最老 entry。"""

    def __init__(self, name: str, max_size: int, default_ttl_seconds: float):
        self.name = name
        self._max_size = int(max_size)
        self._default_ttl = float(default_ttl_seconds)
        self._data: "OrderedDict[Any, tuple[float, Any]]" = OrderedDict()
        self._lock = asyncio.Lock()
        register(self)

    async def get(self, key: K) -> Optional[V]:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            exp, value = entry
            if exp <= time.monotonic():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value  # type: ignore[return-value]

    async def set(self, key: K, value: V, ttl_seconds: Optional[float] = None) -> None:
        ttl = float(ttl_seconds) if ttl_seconds is not None else self._default_ttl
        exp = time.monotonic() + ttl
        async with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (exp, value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    async def pop(self, key: K) -> Optional[V]:
        async with self._lock:
            entry = self._data.pop(key, None)
            return entry[1] if entry is not None else None  # type: ignore[return-value]

    async def get_or_create(self, key: K, factory, ttl_seconds: Optional[float] = None) -> V:
        """获取或惰性创建。factory 是同步可调用，返回 value。"""
        async with self._lock:
            entry = self._data.get(key)
            now = time.monotonic()
            if entry is not None and entry[0] > now:
                self._data.move_to_end(key)
                return entry[1]  # type: ignore[return-value]
            value = factory()
            ttl = float(ttl_seconds) if ttl_seconds is not None else self._default_ttl
            self._data[key] = (now + ttl, value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)
            return value

    async def cleanup(self) -> int:
        now = time.monotonic()
        async with self._lock:
            expired = [k for k, (exp, _v) in self._data.items() if exp <= now]
            for k in expired:
                self._data.pop(k, None)
        return len(expired)

    def size(self) -> int:
        return len(self._data)


class SlidingWindowCounter(_Cleanable):
    """每 key 一个 deque[timestamp]，记录窗口内事件数。

    适用于"每 IP/session/子网 每分钟请求数"这种计数场景。
    """

    def __init__(self, name: str, max_keys: int, window_seconds: float):
        self.name = name
        self._max_keys = int(max_keys)
        self._window = float(window_seconds)
        self._data: "OrderedDict[Any, deque[float]]" = OrderedDict()
        self._lock = asyncio.Lock()
        register(self)

    async def incr_and_count(self, key: Any) -> int:
        """记录一次事件，返回窗口内累计次数。"""
        now = time.monotonic()
        cutoff = now - self._window
        async with self._lock:
            dq = self._data.get(key)
            if dq is None:
                dq = deque()
                self._data[key] = dq
                while len(self._data) > self._max_keys:
                    self._data.popitem(last=False)
            else:
                self._data.move_to_end(key)
            while dq and dq[0] < cutoff:
                dq.popleft()
            dq.append(now)
            return len(dq)

    async def peek_count(self, key: Any) -> int:
        """只读当前计数，不记录新事件。"""
        now = time.monotonic()
        cutoff = now - self._window
        async with self._lock:
            dq = self._data.get(key)
            if dq is None:
                return 0
            while dq and dq[0] < cutoff:
                dq.popleft()
            return len(dq)

    async def cleanup(self) -> int:
        now = time.monotonic()
        cutoff = now - self._window
        async with self._lock:
            removed = 0
            for k in list(self._data.keys()):
                dq = self._data[k]
                while dq and dq[0] < cutoff:
                    dq.popleft()
                if not dq:
                    self._data.pop(k, None)
                    removed += 1
        return removed

    def size(self) -> int:
        return len(self._data)


def stats() -> list[dict[str, Any]]:
    return [{"name": c.name, "size": c.size()} for c in _registry]


async def janitor_loop(interval_seconds: float = 60.0) -> None:
    """统一缓存清理。每 interval 秒扫描全部注册的 cache。"""
    from app.core.logger import logger
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            for c in list(_registry):
                try:
                    await c.cleanup()
                except Exception:
                    logger.exception("cache_cleanup_failed", cache=c.name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cache_janitor_failed")
