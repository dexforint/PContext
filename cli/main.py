from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.config import Config, EnvMode, ensure_app_dirs, load_config, save_config
from ..core.discovery import discover_scripts
from ..core.registry import ScriptRegistry
from ..core.types import ScriptMeta
from ..os_integration.common.shell_open import open_path
from ..os_integration.windows.context_menu import (  # noqa: F401
    get_registered_commands as win_get_registered_commands,
    install_context_menu as win_install_context_menu,
    remove_context_menu as win_remove_context_menu,
)
from ..installers.install_nautilus import (  # noqa: F401
    install_nautilus_script,
    is_nautilus_script_installed,
    remove_nautilus_script,
)

# Небольшие помощники для определения ОС
import platform

IS_WINDOWS = platform.system().lower().startswith("win")
IS_MAC = platform.system().lower().startswith("darwin")
IS_LINUX = platform.system().lower().startswith("linux")


# -----------------------------
# Аргументы
# -----------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcontext",
        description="PContext — инструменты командной строки",
    )
    sub = p.add_subparsers(dest="cmd")

    # os
    p_os = sub.add_parser("os", help="Интеграция с ОС")
    sub_os = p_os.add_subparsers(dest="os_cmd")

    sub_os.add_parser(
        "install-integration", help="Установить интеграцию с контекстным меню"
    )
    sub_os.add_parser(
        "remove-integration", help="Удалить интеграцию с контекстным меню"
    )
    sub_os.add_parser("status", help="Проверить статус интеграции")

    # scripts
    p_scripts = sub.add_parser("scripts", help="Работа со скриптами")
    sub_scripts = p_scripts.add_subparsers(dest="scripts_cmd")
    sub_scripts.add_parser(
        "rescan", help="Пересканировать каталоги скриптов и показать статистику"
    )
    sub_scripts.add_parser("list", help="Показать список обнаруженных скриптов")

    # config
    p_cfg = sub.add_parser("config", help="Настройки PContext")
    sub_cfg = p_cfg.add_subparsers(dest="cfg_cmd")

    sub_cfg.add_parser("show", help="Показать текущую конфигурацию")
    p_add_dir = sub_cfg.add_parser(
        "add-script-dir", help="Добавить каталог со скриптами"
    )
    p_add_dir.add_argument("path", help="Путь к каталогу")
    p_rm_dir = sub_cfg.add_parser("remove-script-dir", help="Удалить каталог из списка")
    p_rm_dir.add_argument("path", help="Путь к каталогу")
    p_set_env = sub_cfg.add_parser(
        "set-env-mode", help="Установить режим окружений (cached/per-script)"
    )
    p_set_env.add_argument(
        "mode", choices=[EnvMode.CACHED.value, EnvMode.PER_SCRIPT.value]
    )
    p_set_auto = sub_cfg.add_parser(
        "set-auto-open", help="Включить/выключить авто-открытие результатов"
    )
    p_set_auto.add_argument("flag", choices=["true", "false"])

    # misc
    p.add_argument("--version", action="store_true", help="Показать версию и выйти")

    return p


# -----------------------------
# Команды ОС
# -----------------------------


def cmd_os_install() -> int:
    try:
        if IS_WINDOWS:
            win_install_context_menu()
            print("Интеграция с контекстным меню Windows установлена (HKCU).")
            return 0
        if IS_LINUX:
            path = install_nautilus_script()
            print(f"Скрипт Nautilus установлен: {path}")
            print("Если пункт не появился — попробуйте: nautilus -q (перезапуск)")
            return 0
        # macOS — пока нет интеграции
        print("Интеграция не поддерживается на данной платформе.")
        return 1
    except Exception as e:
        print(f"Ошибка установки интеграции: {e}", file=sys.stderr)
        return 2


def cmd_os_remove() -> int:
    try:
        if IS_WINDOWS:
            win_remove_context_menu()
            print("Интеграция с контекстным меню Windows удалена.")
            return 0
        if IS_LINUX:
            ok = remove_nautilus_script()
            print(
                "Скрипт Nautilus удален."
                if ok
                else "Не удалось удалить скрипт Nautilus."
            )
            return 0 if ok else 1
        print("Операция не поддерживается на данной платформе.")
        return 1
    except Exception as e:
        print(f"Ошибка удаления интеграции: {e}", file=sys.stderr)
        return 2


def cmd_os_status() -> int:
    try:
        if IS_WINDOWS:
            info = win_get_registered_commands()
            print("Статус интеграции Windows (HKCU):")
            for k in ("files", "dirs", "bg"):
                print(f"  {k:>5}: {info.get(k) or '— не установлено —'}")
            return 0
        if IS_LINUX:
            installed = is_nautilus_script_installed()
            print(
                "Статус интеграции Nautilus:",
                "установлено" if installed else "не установлено",
            )
            if installed:
                print(f"Путь: {Path.home() / '.local/share/nautilus/scripts/PContext'}")
            return 0
        print("Статус: интеграция не поддерживается на данной платформе.")
        return 0
    except Exception as e:
        print(f"Ошибка статуса: {e}", file=sys.stderr)
        return 2


# -----------------------------
# Команды скриптов
# -----------------------------


def _load_registry() -> ScriptRegistry:
    cfg = load_config(create_if_missing=True)
    reg = ScriptRegistry(script_dirs=cfg.script_dirs)
    reg.rescan()
    return reg


def cmd_scripts_rescan() -> int:
    cfg = load_config(create_if_missing=True)
    reg = ScriptRegistry(script_dirs=cfg.script_dirs)
    entries, errors = reg.rescan()
    print(f"Найдено скриптов: {len(entries)}")
    if errors:
        print("Ошибки разборa некоторых файлов:")
        for e in errors:
            print(" -", e)
    return 0


def cmd_scripts_list() -> int:
    reg = _load_registry()
    entries = reg.list_entries()
    if not entries:
        print(
            "Скрипты не найдены. Добавьте .py с метаданными в каталоги из конфигурации."
        )
        return 0
    for e in entries:
        group = (e.meta.group or "").strip()
        print(
            f"- [{e.id}] {(group + ' / ') if group else ''}{e.meta.name}  -> {e.path}"
        )
    return 0


# -----------------------------
# Конфигурация
# -----------------------------


def _print_config(cfg: Config) -> None:
    d = cfg.to_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2))


def cmd_config_show() -> int:
    cfg = load_config(create_if_missing=True)
    _print_config(cfg)
    return 0


def cmd_config_add_dir(path: str) -> int:
    cfg = load_config(create_if_missing=True)
    p = Path(path).expanduser()
    if p in cfg.script_dirs:
        print(f"Каталог уже в списке: {p}")
        return 0
    cfg.script_dirs.append(p)
    save_config(cfg)
    print(f"Добавлен каталог: {p}")
    return 0


def cmd_config_remove_dir(path: str) -> int:
    cfg = load_config(create_if_missing=True)
    p = Path(path).expanduser()
    before = len(cfg.script_dirs)
    cfg.script_dirs = [d for d in cfg.script_dirs if Path(d) != p]
    if len(cfg.script_dirs) == before:
        print(f"Каталог не найден в конфигурации: {p}")
        return 1
    save_config(cfg)
    print(f"Удален каталог: {p}")
    return 0


def cmd_config_set_env_mode(mode: str) -> int:
    cfg = load_config(create_if_missing=True)
    cfg.env_mode = EnvMode(mode)
    save_config(cfg)
    print(f"env_mode = {cfg.env_mode.value}")
    return 0


def cmd_config_set_auto_open(flag: str) -> int:
    cfg = load_config(create_if_missing=True)
    val = flag.strip().lower() in ("1", "true", "yes", "on", "y")
    cfg.auto_open_result = bool(val)
    save_config(cfg)
    print(f"auto_open_result = {cfg.auto_open_result}")
    return 0


# -----------------------------
# main
# -----------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)

    if ns.version:
        # Версию можно читать из пакета (заглушка здесь)
        print("PContext version 0.1.0")
        return 0

    if ns.cmd == "os":
        if ns.os_cmd == "install-integration":
            return cmd_os_install()
        if ns.os_cmd == "remove-integration":
            return cmd_os_remove()
        if ns.os_cmd == "status":
            return cmd_os_status()
        parser.error("Неизвестная команда: os " + str(ns.os_cmd))

    if ns.cmd == "scripts":
        if ns.scripts_cmd == "rescan":
            return cmd_scripts_rescan()
        if ns.scripts_cmd == "list":
            return cmd_scripts_list()
        parser.error("Неизвестная команда: scripts " + str(ns.scripts_cmd))

    if ns.cmd == "config":
        if ns.cfg_cmd == "show":
            return cmd_config_show()
        if ns.cfg_cmd == "add-script-dir":
            return cmd_config_add_dir(ns.path)
        if ns.cfg_cmd == "remove-script-dir":
            return cmd_config_remove_dir(ns.path)
        if ns.cfg_cmd == "set-env-mode":
            return cmd_config_set_env_mode(ns.mode)
        if ns.cfg_cmd == "set-auto-open":
            return cmd_config_set_auto_open(ns.flag)
        parser.error("Неизвестная команда: config " + str(ns.cfg_cmd))

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
