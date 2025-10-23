from __future__ import annotations

import atexit
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from ..core.config import data_dir, ensure_app_dirs
from ..os_integration.common.shell_open import open_path


LOCK_FILE = "pcontextd.pid"


def _lock_path() -> Path:
    return data_dir() / LOCK_FILE


def _read_pid(path: Path) -> Optional[int]:
    try:
        txt = path.read_text(encoding="utf-8").strip()
        if not txt:
            return None
        return int(txt)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    # Пытаемся надежно определить, жив ли процесс.
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        pass

    # POSIX: os.kill(pid, 0) не убивает, но проверяет существование
    if os.name != "nt":
        try:
            os.kill(pid, 0)  # type: ignore[arg-type]
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Процесс есть, но нет прав — считаем живым
            return True
        except Exception:
            return False

    # Windows без psutil — надежно проверить нельзя, считаем не живым
    return False


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def _remove_lock(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _ensure_single_instance() -> bool:
    """
    Гарантирует единственный экземпляр демона на пользователя.
    Возвращает True, если можно продолжать запуск; False, если уже запущен.
    """
    lp = _lock_path()
    if lp.exists():
        pid = _read_pid(lp)
        if pid and _pid_alive(pid):
            # Уже запущен — выходим спокойно
            print("PContext daemon уже запущен.", file=sys.stderr)
            return False
        # Лок-файл «протух» — удалим
        _remove_lock(lp)
    _write_pid(lp, os.getpid())
    atexit.register(lambda: _remove_lock(lp))
    return True


def _install_signal_handlers() -> None:
    # Нежно завершаем Qt-приложение по SIGINT/SIGTERM на поддерживаемых платформах
    try:
        import signal as _sig

        def _handler(signum, frame):
            try:
                from PySide6 import QtWidgets  # type: ignore

                app = QtWidgets.QApplication.instance()  # type: ignore[attr-defined]
                if app is not None:
                    app.quit()  # type: ignore[attr-defined]
            except Exception:
                pass

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                _sig.signal(s, _handler)
            except Exception:
                pass
    except Exception:
        pass


def start_tray() -> int:
    """
    Запускает трей-приложение (PySide6).
    """
    try:
        # Ленивая зависимость — импортируем здесь
        from ..ui.tray.tray_app import main as tray_main  # type: ignore
    except Exception as e:
        sys.stderr.write(
            "Ошибка: для демона/трея требуется PySide6.\n"
            "Установите пакет: pip install PySide6\n"
            f"Детали: {e}\n"
        )
        return 2

    _install_signal_handlers()
    return tray_main()


def main(argv: Optional[list[str]] = None) -> int:
    ensure_app_dirs()
    if not _ensure_single_instance():
        # Можно, например, открыть папку логов/конфиг по второму запуску —
        # но по умолчанию просто тихо завершимся.
        return 0

    # Запускаем трей
    code = start_tray()
    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
