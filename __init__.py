from __future__ import annotations

"""
PContext — библиотека для интеграции пользовательских Python-скриптов
в контекстное меню Windows и Linux (Nautilus Scripts), с запуском в
изолированных окружениях и удобным авто-открытием результатов.

Быстрый старт (CLI):
  - Установить интеграцию с ОС:
      python -m pcontext.cli.main os install-integration
  - Посмотреть список скриптов:
      python -m pcontext.cli.main scripts list
  - Вызов из контекстного меню добавляет пункт «PContext…», который
    открывает компактное окно выбора подходящих скриптов.

Структура метаданных в skrypt.py — см. pcontext.core.metadata / pcontext.core.types.
"""

import platform
from typing import Optional

__all__ = [
    "__version__",
    "get_version",
    "install_integration",
    "remove_integration",
    "integration_status",
]

__version__ = "0.1.0"


def get_version() -> str:
    return __version__


def install_integration() -> None:
    """
    Устанавливает системную интеграцию PContext:
      - Windows: пункты контекстного меню (HKCU)
      - Linux: Nautilus Script (~/.local/share/nautilus/scripts/PContext)
    macOS — пока не поддерживается.
    """
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        from .os_integration.windows.context_menu import install_context_menu

        install_context_menu()
        return
    if sysname.startswith("linux"):
        from .installers.install_nautilus import install_nautilus_script

        install_nautilus_script()
        return
    raise RuntimeError(
        "Интеграция поддерживается только на Windows и Linux (Nautilus)."
    )


def remove_integration() -> None:
    """
    Удаляет системную интеграцию PContext.
    """
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        from .os_integration.windows.context_menu import remove_context_menu

        remove_context_menu()
        return
    if sysname.startswith("linux"):
        from .installers.install_nautilus import remove_nautilus_script

        remove_nautilus_script()
        return
    raise RuntimeError(
        "Удаление интеграции поддерживается только на Windows и Linux (Nautilus)."
    )


def integration_status() -> str:
    """
    Возвращает строку со статусом интеграции для текущей платформы.
    """
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        from .os_integration.windows.context_menu import get_registered_commands

        info = get_registered_commands()
        files = info.get("files") or "не установлено"
        dirs = info.get("dirs") or "не установлено"
        bg = info.get("bg") or "не установлено"
        return f"Windows integration — files: {files}; dirs: {dirs}; bg: {bg}"
    if sysname.startswith("linux"):
        from .installers.install_nautilus import (
            is_nautilus_script_installed,
            script_path,
        )

        ok = is_nautilus_script_installed()
        return f"Nautilus integration — {'установлено' if ok else 'не установлено'} ({script_path()})"
    return "Интеграция не поддерживается на данной платформе."
