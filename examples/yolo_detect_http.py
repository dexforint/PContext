"""
---
name: "YOLO v8 — детекция (HTTP)"
type: "one-shot"
description: "Отправляет изображения на локальный YOLO HTTP-сервис и возвращает аннотированные изображения."
group: "Vision/YOLO"
accepts:
  scope: "files"
  mimes: ["image/*"]
  count: ">=1"
depends:
  pip: ["requests>=2.28"]
params:
  server_url:
    type: "str"
    title: "URL сервиса"
    default: "http://127.0.0.1:5005"
    description: "Базовый URL запущенного yolo_service_http (см. пример)."
timeout:
  one_shot_seconds: 1800
auto_open_result: true
---
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import requests  # type: ignore


def _print_notice(msg: str) -> None:
    print(f"PCTX:NOTICE {msg}", flush=True)


def _print_progress(frac: float) -> None:
    try:
        frac = float(frac)
    except Exception:
        frac = 0.0
    frac = 0.0 if frac < 0 else (1.0 if frac > 1.0 else frac)
    print(f"PCTX:PROGRESS {frac:.6f}", flush=True)


def pcontext_run(
    inputs: List[Dict[str, Any]], params: Dict[str, Any], ctx: Dict[str, Any]
) -> List[Dict[str, str]]:
    """
    Отправляет выбранные изображения на сервис /detect; сохраняет полученные PNG в tmp_dir и возвращает пути.
    """
    base_url = (params.get("server_url") or "http://127.0.0.1:5005").strip().rstrip("/")
    detect_url = f"{base_url}/detect"

    files: List[Path] = []
    for item in inputs:
        if str(item.get("type")) != "file":
            continue
        p = Path(item.get("path", "")).expanduser()
        if p.exists() and p.is_file():
            files.append(p)

    if not files:
        raise RuntimeError("Не выбрано ни одного изображения")

    out_dir = Path(ctx.get("tmp_dir") or Path.cwd()) / "yolo_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, str]] = []
    total = len(files)
    for idx, p in enumerate(files, start=1):
        _print_notice(f"Детекция: {p.name}")
        _print_progress((idx - 1) / max(1, total))
        try:
            with p.open("rb") as f:
                resp = requests.post(
                    detect_url, files={"file": (p.name, f)}, timeout=600
                )
            resp.raise_for_status()
            # Ожидаем PNG-байты
            data = resp.content
            if not data:
                raise RuntimeError("Пустой ответ сервиса")
            out_path = out_dir / f"det_{p.stem}.png"
            out_path.write_bytes(data)
            results.append({"image": str(out_path)})
            _print_notice(f"Готово: {out_path}")
        except requests.HTTPError as he:
            # Попробуем прочитать JSON-ошибку
            try:
                j = resp.json()
                raise RuntimeError(f"Ошибка сервиса: {j.get('error') or he}") from he  # type: ignore[name-defined]
            except Exception:
                raise RuntimeError(f"HTTP {resp.status_code}: {he}") from he  # type: ignore[name-defined]
        except Exception as e:
            raise RuntimeError(f"Ошибка для '{p.name}': {e}") from e
        finally:
            _print_progress(idx / max(1, total))

    return results
