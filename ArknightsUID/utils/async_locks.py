"""按需创建的 asyncio 锁池（notify 订阅互斥 / player 缓存等共用）。"""

from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary


class KeyedLocks:
    """同键串行、空闲键不累积的按需锁池。

    WeakValueDictionary 只保留仍被持有者引用的锁：请求结束后锁可被回收，
    历史键不随访问量常驻内存；同一时刻并发请求拿到的总是同一把锁。
    """

    __slots__ = ("_locks",)

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock
