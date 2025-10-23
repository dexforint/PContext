from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...core.errors import OSIntegrationError
from ...core.config import is_windows


try:
    import winreg  # type: ignore
except Exception as e:  # pragma: no cover
    winreg = None  # type: ignore


# Реестр (HKCU) — ключи для старого контекстного меню (Windows 10/11 "classic")
# 1) Файлы:      HKCU\Software\Classes\*\shell\PContext\command
# 2) Папки:      HKCU\Software\Classes\Directory\shell\PContext\command
# 3) Пустая обл: HKCU\Software\Classes\Directory\Background\shell\PContext\command

KEYS = {
    "files": r"Software\Classes\*\shell\PContext",
    "dirs": r"Software\Classes\Directory\shell\PContext",
    "bg": r"Software\Classes\Directory\Background\shell\PContext",
}

DEFAULT_NAME = "PContext..."


def _ensure_win() -> None:
    if not is_windows() or winreg is None:
        raise OSIntegrationError("Интеграция с Windows недоступна в текущей среде")


def _default_cli_invocation() -> List[str]:
    """
    Команда по умолчанию для вызова лаунчера.
    Используем текущий интерпретатор Python и модуль pcontext.cli.launcher:
      <python.exe> -m pcontext.cli.launcher
    """
    return [sys.executable, "-m", "pcontext.cli.launcher"]


def _default_icon_path() -> Optional[str]:
    """
    Путь к иконке pcontext.ico из ресурсов пакета.
    Если файл не найден — вернем None (Shell покажет стандартную).
    """
    try:
        # pcontext/os_integration/windows/context_menu.py -> до корня пакета pcontext
        root = Path(__file__).resolve().parents[3]
        icon = root / "resources" / "icons" / "pcontext.ico"
        if icon.exists():
            return str(icon)
    except Exception:
        pass
    return None


def _needs_quoting(s: str) -> bool:
    for ch in s:
        if ch.isspace():
            return True
        if ch in (
            '"',
            "'",
            "&",
            "(",
            ")",
            "[",
            "]",
            "{",
            "}",
            "^",
            "=",
            ";",
            "!",
            "+",
            ",",
            "`",
            "~",
        ):
            return True
    return False


def _quote_arg(s: str) -> str:
    """
    Квотирование аргумента для Windows командной строки.
    Переменные-плейсхолдеры (%1, %*, %V) не квотируем.
    """
    s = str(s)
    if s.startswith("%") and s.endswith("%"):
        return s
    if s == "%*":
        return s
    if not _needs_quoting(s):
        return s
    # Экранируем двойные кавычки
    s = s.replace('"', r"\"")
    return f'"{s}"'


def _join_cmd(parts: List[str]) -> str:
    return " ".join(_quote_arg(p) for p in parts)


def _build_command_cli(cli_invocation: Optional[List[str]], scope: str) -> str:
    """
    Формирует строку команды для записи в реестр.
    scope: "files" | "dirs" | "bg"
    """
    base = list(cli_invocation or _default_cli_invocation())
    base += ["launcher", "--scope", scope]
    if scope == "bg":
        # Для пустой области — передаем текущую директорию
        base += ["--cwd", "%V"]
    elif scope == "dirs":
        # Для директорий — одна директория через %1 (в классическом меню)
        base += ["--paths", "%1"]
    else:
        # Для файлов — все выделенные элементы через %*
        base += ["--paths", "%*"]
    return _join_cmd(base)


def _open_key(path: str, write: bool = False):
    hive = winreg.HKEY_CURRENT_USER
    if write:
        return winreg.CreateKeyEx(hive, path, 0, access=winreg.KEY_WRITE)
    return winreg.OpenKey(hive, path, 0, access=winreg.KEY_READ)


def _set_value(key, name: Optional[str], value, regtype=None) -> None:
    if regtype is None:
        regtype = winreg.REG_SZ
    winreg.SetValueEx(key, name, 0, regtype, value)


def _delete_tree(path: str) -> None:
    """
    Удаляет ключ и все его поддеревья из HKCU.
    """
    try:
        with _open_key(path, write=False) as k:
            # Перечислить под-ключи и удалить их рекурсивно
            index = 0
            subkeys = []
            while True:
                try:
                    name = winreg.EnumKey(k, index)
                    subkeys.append(name)
                    index += 1
                except OSError:
                    break
    except FileNotFoundError:
        return

    # Удаляем детей
    for child in subkeys:
        _delete_tree(path + "\\" + child)

    # Удаляем сам ключ
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def install_context_menu(
    display_name: str = DEFAULT_NAME,
    icon_path: Optional[str] = None,
    cli_invocation: Optional[List[str]] = None,
) -> None:
    """
    Создает/обновляет пункты контекстного меню:
      - для файлов:      *\shell\PContext
      - для директорий:  Directory\shell\PContext
      - для пустой обл.: Directory\Background\shell\PContext

    display_name: подпись пункта меню (например, "PContext...")
    icon_path: абсолютный путь к .ico; если None — берем из ресурсов, если не найден — без иконки
    cli_invocation: список токенов команды для вызова pcontext лаунчера
                    (по умолчанию: [sys.executable, "-m", "pcontext.cli.launcher"])
    """
    _ensure_win()

    icon_path = icon_path or _default_icon_path()
    cmd_files = _build_command_cli(cli_invocation, "files")
    cmd_dirs = _build_command_cli(cli_invocation, "dirs")
    cmd_bg = _build_command_cli(cli_invocation, "bg")

    try:
        # Files
        with _open_key(KEYS["files"], write=True) as k:
            _set_value(k, None, display_name)  # (Default) - подпись
            if icon_path:
                _set_value(k, "Icon", icon_path)
            # небольшая оптимизация — не менять текущую рабочую директорию процесса
            _set_value(k, "NoWorkingDirectory", "")
            with _open_key(KEYS["files"] + r"\command", write=True) as kc:
                _set_value(kc, None, cmd_files)

        # Directories
        with _open_key(KEYS["dirs"], write=True) as k:
            _set_value(k, None, display_name)
            if icon_path:
                _set_value(k, "Icon", icon_path)
            _set_value(k, "NoWorkingDirectory", "")
            with _open_key(KEYS["dirs"] + r"\command", write=True) as kc:
                _set_value(kc, None, cmd_dirs)

        # Background
        with _open_key(KEYS["bg"], write=True) as k:
            _set_value(k, None, display_name)
            if icon_path:
                _set_value(k, "Icon", icon_path)
            _set_value(k, "NoWorkingDirectory", "")
            with _open_key(KEYS["bg"] + r"\command", write=True) as kc:
                _set_value(kc, None, cmd_bg)

    except PermissionError as e:
        # HKCU не требует админ-прав — но на корпоративных машинах политика может ограничивать
        raise OSIntegrationError(f"Нет прав для записи в реестр HKCU: {e}") from e
    except FileNotFoundError as e:
        raise OSIntegrationError(f"Не удалось создать ключи реестра: {e}") from e
    except OSError as e:
        raise OSIntegrationError(f"Ошибка записи в реестр: {e}") from e


def remove_context_menu() -> None:
    """
    Удаляет все пункты «PContext» из контекстного меню текущего пользователя.
    """
    _ensure_win()
    for path in KEYS.values():
        try:
            _delete_tree(path)
        except Exception:
            # не критично
            pass


def get_registered_commands() -> Dict[str, Optional[str]]:
    """
    Возвращает текущие команды, записанные в реестре, для диагностики.
    Ключи: "files", "dirs", "bg"
    """
    _ensure_win()
    out: Dict[str, Optional[str]] = {"files": None, "dirs": None, "bg": None}
    for key_name, path in KEYS.items():
        try:
            with _open_key(path + r"\command", write=False) as k:
                val, _typ = winreg.QueryValueEx(k, None)
                out[key_name] = str(val)
        except FileNotFoundError:
            out[key_name] = None
        except Exception:
            out[key_name] = None
    return out


__all__ = [
    "install_context_menu",
    "remove_context_menu",
    "get_registered_commands",
]
