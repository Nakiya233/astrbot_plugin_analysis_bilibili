"""解析去重缓存。

移植自 nonebot_plugin_analysis_bilibili 的 ``ExpiringCache``。
"""

from __future__ import annotations

import time
from typing import Any


class ExpiringCache:
    """一个按插入顺序过期的极简缓存。

    - ``expire_seconds <= 0`` 时只记录最后一个值（原插件默认行为）；
    - ``expire_seconds > 0`` 时记录多个值，并在每次读写时淘汰过期项。
    """

    def __init__(self, expire_seconds: float = 0) -> None:
        self.cache: dict[Any, float] = {}
        self.expire_seconds = expire_seconds

    def _evict_expired(self) -> None:
        if self.expire_seconds <= 0:
            return
        now = time.monotonic()
        while self.cache:
            oldest_key = next(iter(self.cache))
            if self.cache[oldest_key] <= now:
                del self.cache[oldest_key]
            else:
                break

    def set(self, value: Any) -> None:
        """记录一个值。"""
        self._evict_expired()
        if value in self.cache:
            return
        if self.expire_seconds <= 0:
            self.cache.clear()
            self.cache[value] = 0.0
        else:
            self.cache[value] = time.monotonic() + self.expire_seconds

    def get(self, value: Any) -> Any | None:
        """获取一个值，不存在或已过期时返回 ``None``。"""
        exp = self.cache.get(value)
        if exp is None:
            return None
        if self.expire_seconds > 0 and exp <= time.monotonic():
            del self.cache[value]
            return None
        return value

    def __str__(self) -> str:
        self._evict_expired()
        return str(set(self.cache.keys()))
