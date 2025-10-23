from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional, Union

from ...core.types import is_url


def _creation_flags() -> int:
    """
    Флаги для скрытого запуска процессов в Windows (без мигающего консольного окна).
    """
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _spawn(cmd: list[str], cwd: Optional[Union[str, Path]] = None) -> bool:
    """
    Запускает внешнюю команду и немедленно возвращает управление.
    Возвращает True, если запуск прошел без исключений.
    """
    try:
        subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        return True
    except Exception:
        return False


def open_path(path: Union[str, Path]) -> bool:
    """
    Открывает файл/папку в ассоциированной программе ОС.
    - Windows: os.startfile
    - Linux: xdg-open
    - macOS: open
    Возвращает True при успехе.
    """
    p = str(path)
    try:
        if os.name == "nt":
            os.startfile(p)  # type: ignore[attr-defined]
            return True
        elif sys.platform == "darwin":
            return _spawn(["open", p])
        else:
            # Linux/BSD
            return _spawn(["xdg-open", p])
    except Exception:
        return False


def open_url(url: str) -> bool:
    """
    Открывает ссылку в браузере по умолчанию.
    На Windows os.startfile умеет URL, но используем webbrowser для универсальности.
    """
    try:
        if os.name == "nt":
            # os.startfile часто быстрее открывает системный браузер
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        # webbrowser.open вернет True, если сумел найти браузер
        ok = webbrowser.open(
            url, new=2
        )  # new=2 — открыть в новом окне/вкладке если возможно
        return bool(ok)
    except Exception:
        # Фолбэк — попробуем через системные команды
        if sys.platform == "darwin":
            return _spawn(["open", url])
        else:
            return _spawn(["xdg-open", url])


def open_any(path_or_url: str) -> bool:
    """
    Универсальный открыватель: если это URL — откроет в браузере,
    иначе попытается открыть путь в системной программе.
    """
    if is_url(path_or_url):
        return open_url(path_or_url)
    return open_path(path_or_url)


def reveal_in_file_manager(path: Union[str, Path]) -> bool:
    """
    Показывает файл/папку в файловом менеджере:
    - Windows: Explorer с выделением файла (если файл) или просто открыть папку.
    - macOS: open -R для файла или open для папки.
    - Linux: xdg-open для папки; если файл — открываем его родительскую папку.
    """
    p = Path(path)
    try:
        if os.name == "nt":
            if p.is_dir():
                return _spawn(["explorer", str(p)])
            # explorer /select, <path> — требует обратные слеши
            return _spawn(["explorer", "/select,", str(p)])
        elif sys.platform == "darwin":
            if p.is_file():
                return _spawn(["open", "-R", str(p)])
            return _spawn(["open", str(p)])
        else:
            folder = p if p.is_dir() else p.parent
            return _spawn(["xdg-open", str(folder)])
    except Exception:
        return False


__all__ = [
    "open_path",
    "open_url",
    "open_any",
    "reveal_in_file_manager",
]
