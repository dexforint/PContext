"""
---
name: "Загрузить на temp.sh"
type: "one-shot"
description: "Загружает выбранные файлы на https://temp.sh (до 4 ГБ на файл) и возвращает ссылки."
group: "Network/Upload"
accepts:
  scope: "files"
  count: ">=1"
depends:
  pip: ["requests>=2.28"]
params:
  server_url:
    type: "str"
    title: "URL сервера"
    default: "https://temp.sh/upload"
    description: "Точка загрузки (по умолчанию temp.sh)."
  use_curl:
    type: "bool"
    title: "Использовать curl вместо requests"
    default: false
timeout:
  one_shot_seconds: 1800
auto_open_result: true
---
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_FILE_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4 ГБ


def _is_tool_available(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _stat_size(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except Exception:
        return None


def _upload_via_requests(file_path: Path, server_url: str, timeout: int = 600) -> str:
    import requests  # type: ignore

    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f)}
        # temp.sh возвращает в теле ответа ссылку (text/plain)
        resp = requests.post(server_url, files=files, timeout=timeout)
    resp.raise_for_status()
    url = (resp.text or "").strip()
    if not url:
        raise RuntimeError("Сервер не вернул ссылку (пустой ответ)")
    return url


def _upload_via_curl(file_path: Path, server_url: str, timeout: int = 600) -> str:
    if not _is_tool_available("curl"):
        raise RuntimeError(
            "curl не найден в системе, отключите параметр 'use_curl' или установите curl"
        )
    # -fS: fail+show errors, -L: follow redirects
    cmd = [
        "curl",
        "-fS",
        "-L",
        "-m",
        str(timeout),
        "-F",
        f"file=@{str(file_path)}",
        server_url,
    ]
    # Важно: не используем shell=True, чтобы избежать проблем с кавычками/безопасностью
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if res.returncode != 0:
        # Вернем stderr как причину
        raise RuntimeError(
            f"curl завершился с кодом {res.returncode}: {res.stderr.strip() or res.stdout.strip()}"
        )
    url = (res.stdout or "").strip()
    if not url:
        raise RuntimeError("Сервер не вернул ссылку (пустой stdout)")
    return url


def _print_notice(msg: str) -> None:
    print(f"PCTX:NOTICE {msg}", flush=True)


def _print_progress(frac: float) -> None:
    try:
        frac = float(frac)
    except Exception:
        frac = 0.0
    # 0..1
    frac = 0.0 if frac < 0 else (1.0 if frac > 1.0 else frac)
    print(f"PCTX:PROGRESS {frac:.6f}", flush=True)


def pcontext_run(
    inputs: List[Dict[str, Any]], params: Dict[str, Any], ctx: Dict[str, Any]
) -> List[Dict[str, str]]:
    """
    inputs: [{type,path,name,...}, ...] (ожидаем только type="file")
    params:
      - server_url: str
      - use_curl: bool
    ctx: { tmp_dir, cache_dir, ... }
    """
    server_url = str(params.get("server_url") or "https://temp.sh/upload").strip()
    use_curl = bool(params.get("use_curl", False))

    # Соберем файлы
    files: List[Path] = []
    for item in inputs:
        if str(item.get("type")) != "file":
            # Игнорируем не-файлы (по accepts их не должно быть)
            continue
        p = Path(item.get("path", "")).expanduser()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"Файл не найден: {p}")
        files.append(p)

    if not files:
        raise RuntimeError("Не выбрано ни одного файла")

    # Проверка ограничений размера
    oversize: List[str] = []
    total = len(files)
    for idx, p in enumerate(files, start=1):
        size = _stat_size(p)
        if size is None:
            raise RuntimeError(f"Не удалось определить размер файла: {p}")
        if size > MAX_FILE_SIZE_BYTES:
            oversize.append(f"{p.name} ({size} байт)")
        # Прогресс по подготовке (необязательно)
        _print_progress((idx - 0.5) / max(1, total))
    if oversize:
        raise RuntimeError(
            "Следующие файлы превышают лимит 4 ГБ и не будут загружены:\n- "
            + "\n- ".join(oversize)
        )

    results: List[Dict[str, str]] = []
    for idx, p in enumerate(files, start=1):
        _print_notice(f"Загрузка: {p.name}")
        # Подавать прогресс на уровне файлов (грубый, без прогресса тела запроса)
        _print_progress((idx - 1) / max(1, total))
        try:
            if use_curl:
                url = _upload_via_curl(p, server_url)
            else:
                url = _upload_via_requests(p, server_url)
        except Exception as e:
            raise RuntimeError(f"Ошибка загрузки '{p.name}': {e}") from e
        _print_notice(f"Готово: {p.name} -> {url}")
        results.append({"link": url})
        _print_progress(idx / max(1, total))

    return results
