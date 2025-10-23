"""
---
name: "YOLO v8 — детекция (через сервис)"
type: "one-shot"
description: "Прокси-скрипт: передаёт изображения сервису 'yolo_service' и возвращает аннотированные изображения."
group: "Vision/YOLO"
accepts:
  scope: "files"
  mimes: ["image/*"]
  count: ">=1"
depends:
  scripts: ["yolo_service"]   # зависимость от сервисного скрипта
proxy_service: "yolo_service" # явная подсказка мосту
params:
  # Эти параметры будут проксированы в pcontext_request сервиса
  conf:
    type: "float"
    title: "Confidence threshold (override)"
    default: 0.25
  iou:
    type: "float"
    title: "IOU threshold (override)"
    default: 0.45
  imgsz:
    type: "int"
    title: "Размер изображения (override)"
    default: 640
timeout:
  one_shot_seconds: 1800
auto_open_result: true
---
"""

from __future__ import annotations

from typing import Any, Dict, List


def pcontext_run(
    inputs: List[Dict[str, Any]], params: Dict[str, Any], ctx: Dict[str, Any]
):
    """
    Этот скрипт — декларация «проксировать в сервис yolo_service».
    Лаунчер PContext перехватит вызов и направит inputs/params в сервис.
    Прямой запуск pcontext_run здесь ничего не делает.
    """
    # Лёгкая подсказка в лог на случай прямого запуска:
    print(
        "PCTX:NOTICE Этот скрипт — прокси. Вызов будет направлен в сервис 'yolo_service'.",
        flush=True,
    )
    return None
