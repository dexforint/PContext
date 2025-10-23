from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import PContextError


class ResourceError(PContextError):
    pass


class ResourceTimeout(ResourceError):
    pass


class _LockBucket:
    """
    Примитив блокировки с ограничением параллелизма (семафор) для одного «ресурса».
    Реализован поверх threading.Semaphore, чтобы уметь увеличивать capacity на лету.

    Ограничения:
    - Уменьшение capacity вниз не реализовано (безопасно только увеличение).
    """

    def __init__(self, name: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.name = name
        self.capacity = int(capacity)
        self._sem = threading.Semaphore(0)
        self._mu = threading.Lock()
        self._in_use = 0
        # Инициализируем токены
        for _ in range(self.capacity):
            self._sem.release()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        ok = self._sem.acquire(timeout=timeout)
        if not ok:
            return False
        with self._mu:
            self._in_use += 1
        return True

    def release(self) -> None:
        with self._mu:
            if self._in_use <= 0:
                # Защита от «лишних» release
                raise ResourceError(f"release overflow for lock '{self.name}'")
            self._in_use -= 1
        self._sem.release()

    def increase_capacity(self, delta: int) -> None:
        if delta <= 0:
            return
        with self._mu:
            self.capacity += int(delta)
        for _ in range(delta):
            self._sem.release()

    @property
    def in_use(self) -> int:
        with self._mu:
            return self._in_use

    @property
    def available(self) -> int:
        with self._mu:
            return max(0, self.capacity - self._in_use)


@dataclass
class ResourceGuard:
    """
    Контекстный менеджер/токен удержания набора замков.
    Освобождает все захваченные замки при выходе из контекста или при явном release().
    """

    pool: "ResourcePool"
    locks: List[str]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self.pool._release_many(self.locks)
        self._released = True

    # контекстный менеджер
    def __enter__(self) -> "ResourceGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.release()


class ResourcePool:
    """
    Пул ресурсных «замков» с ограничением параллелизма по имени.
    Использование:
      pool = ResourcePool({"GPU": 1})
      with pool.acquire(["GPU"], timeout=30):
          ... выполнять работу, требующую эксклюзивного GPU ...

    Политика:
      - Для неизвестного имени замка применяется default_capacity (по умолчанию 1).
      - Изменение политики (update_policy) поддерживает добавление новых замков и увеличение capacity существующих.
        Уменьшение capacity безопасно не реализовано.
      - Для предотвращения дедлоков набор замков всегда захватывается в стабильном порядке по имени.
    """

    def __init__(
        self, policy: Optional[Mapping[str, int]] = None, default_capacity: int = 1
    ) -> None:
        if default_capacity < 1:
            raise ValueError("default_capacity must be >= 1")
        self._default_capacity = int(default_capacity)
        self._buckets: Dict[str, _LockBucket] = {}
        self._mu = threading.RLock()
        if policy:
            for name, cap in policy.items():
                self._ensure_bucket(str(name), int(cap if cap and cap > 0 else 1))

    # -----------------------------
    # Политика/настройка
    # -----------------------------

    def update_policy(self, policy: Mapping[str, int]) -> None:
        """
        Обновляет политику:
          - добавляет новые замки,
          - увеличивает capacity существующих (при необходимости).
        Уменьшение capacity не поддерживается.
        """
        with self._mu:
            for name, cap in policy.items():
                cap = int(cap if cap and cap > 0 else 1)
                b = self._buckets.get(name)
                if b is None:
                    self._buckets[name] = _LockBucket(name, cap)
                else:
                    if cap > b.capacity:
                        b.increase_capacity(cap - b.capacity)

    def info(self) -> Dict[str, Dict[str, int]]:
        """
        Диагностическая информация по доступным замкам: capacity, in_use, available.
        """
        out: Dict[str, Dict[str, int]] = {}
        with self._mu:
            for name, b in sorted(self._buckets.items()):
                out[name] = {
                    "capacity": b.capacity,
                    "in_use": b.in_use,
                    "available": b.available,
                }
        return out

    # -----------------------------
    # Захват/освобождение
    # -----------------------------

    def try_acquire(
        self, locks: Sequence[str], timeout: Optional[float] = None
    ) -> Optional[ResourceGuard]:
        """
        Пытается захватить набор замков за отведенное время. Возвращает ResourceGuard или None по таймауту.
        Дубликаты имен игнорируются.
        """
        acquired: List[str] = []
        if not locks:
            return ResourceGuard(self, [])  # ничего захватывать не нужно

        # Подготовим отсортированный список уникальных замков
        names = sorted({str(x).strip() for x in locks if str(x).strip()})
        # Пошаговый захват с дедлайном
        deadline = (
            None if timeout is None else (time.monotonic() + max(0.0, float(timeout)))
        )
        for name in names:
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
            if remaining is not None and remaining == 0.0:
                # Время вышло
                self._release_many(acquired)
                return None
            b = self._ensure_bucket(name, None)
            ok = b.acquire(timeout=remaining)
            if not ok:
                # Не удалось взять этот замок — откатываем предыдущие
                self._release_many(acquired)
                return None
            acquired.append(name)

        return ResourceGuard(self, acquired)

    def acquire(
        self, locks: Sequence[str], timeout: Optional[float] = None
    ) -> ResourceGuard:
        """
        Как try_acquire, но бросает ResourceTimeout при неудаче.
        """
        g = self.try_acquire(locks, timeout=timeout)
        if g is None:
            raise ResourceTimeout(
                f"Не удалось получить замки: {', '.join(sorted(set(map(str, locks))))}"
            )
        return g

    def _release_many(self, locks: Sequence[str]) -> None:
        # Освобождаем в обратном порядке
        for name in reversed(list(locks)):
            b = self._buckets.get(name)
            if b is None:
                # Нечего освобождать — защитимся от рассинхронизации
                continue
            b.release()

    # -----------------------------
    # Внутреннее
    # -----------------------------

    def _ensure_bucket(self, name: str, cap: Optional[int]) -> _LockBucket:
        with self._mu:
            b = self._buckets.get(name)
            if b is None:
                capacity = int(cap if cap and cap > 0 else self._default_capacity)
                b = _LockBucket(name, capacity)
                self._buckets[name] = b
            return b


__all__ = [
    "ResourcePool",
    "ResourceGuard",
    "ResourceError",
    "ResourceTimeout",
]
