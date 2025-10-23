from __future__ import annotations

from typing import Dict, List, Optional

from ..core.config import is_windows
from ..core.errors import OSIntegrationError
from ..os_integration.windows.context_menu import (
    get_registered_commands,
    install_context_menu,
    remove_context_menu,
)


def install_windows_integration(
    display_name: str = "PContext...",
    icon_path: Optional[str] = None,
    cli_invocation: Optional[List[str]] = None,
) -> None:
    """
    Устанавливает интеграцию PContext в контекстное меню Windows (классическое меню Win10/Win11).
    Создает пункт 'PContext...' для:
      - файлов:      HKCU\\Software\\Classes\\*\\shell\\PContext
      - папок:       HKCU\\Software\\Classes\\Directory\\shell\\PContext
      - пустой обл.: HKCU\\Software\\Classes\\Directory\\Background\\shell\\PContext

    display_name — подпись пункта меню.
    icon_path    — путь к .ico для иконки пункта (опционально).
    cli_invocation — как вызывать лаунчер, по умолчанию: [sys.executable, "-m", "pcontext.cli.launcher"].
    """
    if not is_windows():
        raise OSIntegrationError("Интеграция Windows доступна только на Windows.")
    install_context_menu(
        display_name=display_name, icon_path=icon_path, cli_invocation=cli_invocation
    )


def remove_windows_integration() -> None:
    """
    Удаляет все пункты интеграции PContext из контекстного меню текущего пользователя (HKCU).
    """
    if not is_windows():
        raise OSIntegrationError(
            "Удаление интеграции Windows доступно только на Windows."
        )
    remove_context_menu()


def windows_integration_status() -> Dict[str, Optional[str]]:
    """
    Возвращает текущее состояние записей в реестре для контекстного меню.
    Ключи: "files", "dirs", "bg". Значения — командные строки или None.
    """
    if not is_windows():
        raise OSIntegrationError(
            "Статус интеграции Windows доступен только на Windows."
        )
    return get_registered_commands()


__all__ = [
    "install_windows_integration",
    "remove_windows_integration",
    "windows_integration_status",
]
