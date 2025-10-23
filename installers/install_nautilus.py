from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

from ..core.errors import OSIntegrationError


NAUTILUS_SCRIPTS_DIR = Path.home() / ".local" / "share" / "nautilus" / "scripts"
DEFAULT_SCRIPT_NAME = "PContext"


SCRIPT_CONTENT = r"""#!/usr/bin/env bash
# Nautilus Script: PContext
# Устанавливается PContext для вызова лаунчера через контекстное меню Nautilus.
# Требуется установленный Python 3 с модулем pcontext.

set -euo pipefail

# Соберем список выделенных путей из переменной окружения Nautilus:
# NAUTILUS_SCRIPT_SELECTED_FILE_PATHS — список путей, разделенных переводами строк.
IFS=$'\n' read -r -d '' -a SEL_PATHS < <(printf "%s\0" "${NAUTILUS_SCRIPT_SELECTED_FILE_PATHS:-}")

scope="mixed"
if [ "${#SEL_PATHS[@]}" -eq 0 ]; then
  scope="background"
else
  all_files=1
  all_dirs=1
  for p in "${SEL_PATHS[@]}"; do
    if [ -d "$p" ]; then
      all_files=0
    else
      all_dirs=0
    fi
  done
  if [ $all_files -eq 1 ]; then
    scope="files"
  elif [ $all_dirs -eq 1 ]; then
    scope="dirs"
  else
    scope="mixed"
  fi
fi

# Определим текущую директорию для background-кейса через NAUTILUS_SCRIPT_CURRENT_URI.
cwd_arg=()
if [ "$scope" = "background" ] && [ -n "${NAUTILUS_SCRIPT_CURRENT_URI:-}" ]; then
  # Преобразуем file:// URI в путь
  PY_CODE='import os,sys,urllib.parse;u=os.environ.get("NAUTILUS_SCRIPT_CURRENT_URI","");p="";'\
'u=u.strip();'\
'print(urllib.parse.unquote(u[7:]) if u.startswith("file://") else "", end="")'
  CWD=$(python3 -c "$PY_CODE" 2>/dev/null || true)
  if [ -n "$CWD" ]; then
    cwd_arg=(--cwd "$CWD")
  fi
fi

# Запуск pcontext лаунчера
# Используем python3 -m pcontext.cli.launcher для надежности.
# Передаем все выбранные пути как --paths.
exec /usr/bin/env python3 -m pcontext.cli.launcher launcher --scope "$scope" "${cwd_arg[@]}" --paths "${SEL_PATHS[@]}"
"""


def nautilus_scripts_dir() -> Path:
    return NAUTILUS_SCRIPTS_DIR


def script_path(name: str = DEFAULT_SCRIPT_NAME) -> Path:
    return nautilus_scripts_dir() / name


def install_nautilus_script(
    name: str = DEFAULT_SCRIPT_NAME, overwrite: bool = True
) -> Path:
    """
    Устанавливает Nautilus Script "~/.local/share/nautilus/scripts/<name>" для запуска PContext.
    - name: отображаемое имя пункта (имя файла скрипта в меню Scripts).
    - overwrite: перезаписать при существовании.

    Возвращает путь к установленному скрипту.
    """
    if os.name == "nt":
        raise OSIntegrationError(
            "Установка Nautilus Script поддерживается только на Linux/Unix."
        )

    target_dir = nautilus_scripts_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    target = script_path(name)
    if target.exists() and not overwrite:
        return target

    target.write_text(SCRIPT_CONTENT, encoding="utf-8")
    # Сделать исполняемым: chmod +x
    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def remove_nautilus_script(name: str = DEFAULT_SCRIPT_NAME) -> bool:
    """
    Удаляет установленный Nautilus Script. Возвращает True при успехе/отсутствии файла.
    """
    if os.name == "nt":
        raise OSIntegrationError(
            "Удаление Nautilus Script поддерживается только на Linux/Unix."
        )
    p = script_path(name)
    try:
        p.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def is_nautilus_script_installed(name: str = DEFAULT_SCRIPT_NAME) -> bool:
    """
    Проверяет, установлен ли скрипт.
    """
    return script_path(name).exists()


__all__ = [
    "install_nautilus_script",
    "remove_nautilus_script",
    "is_nautilus_script_installed",
    "nautilus_scripts_dir",
    "script_path",
]
