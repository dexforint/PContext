from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from ...core.discovery import ChangeType, FileChangeEvent, ScriptDirWatcher


class DirWatchController:
    """
    Обертка вокруг ScriptDirWatcher с дебаунсом событий.
    Предназначена для UI (трей): при изменении .py в каталогах скриптов
    вызывает on_changed() не чаще, чем раз в debounce_ms миллисекунд.

    Использование:
      def on_changed():
          # пересканировать скрипты, обновить меню/список
          ...

      watcher = DirWatchController(script_dirs, on_changed, debounce_ms=400)
      watcher.start()
      ...
      watcher.stop()
    """

    def __init__(
        self,
        script_dirs: Sequence[Path],
        on_changed: Callable[[], None],
        debounce_ms: int = 400,
    ) -> None:
        self.script_dirs = [Path(p) for p in script_dirs]
        self.on_changed = on_changed
        self.debounce_ms = max(0, int(debounce_ms))

        self._watcher: Optional[ScriptDirWatcher] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._active = False

    def _schedule_callback(self) -> None:
        """
        Планирует вызов on_changed() через debounce-интервал.
        Несколько событий внутри интервала схлопываются в один вызов.
        """
        with self._lock:
            # Отменим существующий таймер, если он есть
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None
            # Запланируем новый
            self._timer = threading.Timer(
                self.debounce_ms / 1000.0, self._fire_callback
            )
            self._timer.daemon = True
            self._timer.start()

    def _fire_callback(self) -> None:
        # Разрешаем создать новый таймер
        with self._lock:
            self._timer = None
        try:
            self.on_changed()
        except Exception:
            # UI-слой сам решит, как логировать ошибки
            pass

    def _on_fs_event(self, ev: FileChangeEvent) -> None:
        # Любое событие .py файла — достаточно, чтобы инициировать перескан
        self._schedule_callback()

    def start(self) -> bool:
        """
        Запускает наблюдатель. Возвращает True при успехе (watchdog установлен).
        """
        if self._active:
            return True
        try:
            self._watcher = ScriptDirWatcher(
                self.script_dirs, callback=self._on_fs_event, recursive=True
            )
            self._watcher.start()
            self._active = True
            return True
        except RuntimeError:
            # watchdog не установлен — тихо отключим наблюдение
            self._watcher = None
            self._active = False
            return False
        except Exception:
            self._watcher = None
            self._active = False
            return False

    def stop(self) -> None:
        """
        Останавливает наблюдение и отменяет запланированные вызовы.
        """
        try:
            if self._watcher is not None:
                self._watcher.stop()
        except Exception:
            pass
        finally:
            self._watcher = None
            self._active = False
        # Остановим таймер дебаунса
        with self._lock:
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

    def is_active(self) -> bool:
        return bool(self._active)


__all__ = [
    "DirWatchController",
]
