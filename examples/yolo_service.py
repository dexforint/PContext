"""
---
id: "yolo_service"
name: "YOLO v8 — сервис"
type: "service"
description: "Долго живущий процесс с загруженной YOLO-моделью. Обрабатывает запросы детекции."
group: "Vision/YOLO"
accepts:
  scope: "background"
depends:
  pip: ["ultralytics>=8.0", "torch", "pillow>=9.5", "numpy>=1.23"]
params:
  model:
    type: "str"
    title: "Весы модели"
    default: "yolov8n.pt"
  device:
    type: "str"
    title: "Устройство"
    default: "cpu"
    description: "Например: cpu, cuda:0"
  half:
    type: "bool"
    title: "Half precision (FP16)"
    default: false
  conf:
    type: "float"
    title: "Confidence threshold"
    default: 0.25
    min: 0.0
    max: 1.0
    step: 0.01
  iou:
    type: "float"
    title: "IOU threshold"
    default: 0.45
    min: 0.0
    max: 1.0
    step: 0.01
  imgsz:
    type: "int"
    title: "Размер изображения (imgsz)"
    default: 640
timeout:
  service_idle_seconds: 600
  grace_seconds: 5
auto_open_result: false
---
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Глобальное состояние сервиса (живет в отдельном процессе)
_model = None
_device: str = "cpu"
_half: bool = False
_conf: float = 0.25
_iou: float = 0.45
_imgsz: int = 640


def _notice(msg: str) -> None:
    # Для логов/прогресса в PContext
    print(f"PCTX:NOTICE {msg}", flush=True)


def _ensure_out_dir(ctx: Dict[str, Any]) -> Path:
    out_dir = Path(ctx.get("tmp_dir") or Path.cwd()) / "yolo_service_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def pcontext_init(params: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """
    Разовая инициализация сервиса: загрузка модели YOLO и сохранение настроек.
    """
    global _model, _device, _half, _conf, _iou, _imgsz

    _device = str(params.get("device") or "cpu").strip()
    _half = bool(params.get("half", False))
    _conf = float(params.get("conf", 0.25))
    _iou = float(params.get("iou", 0.45))
    _imgsz = int(params.get("imgsz", 640))
    model_name = str(params.get("model") or "yolov8n.pt").strip()

    _notice(f"Загрузка модели: {model_name} (device={_device}, half={_half})")
    from ultralytics import YOLO

    m = YOLO(model_name)
    # Перенос на устройство
    try:
        m.to(_device)
    except Exception:
        # Некоторые версии требуют device только при вызове; игнорируем ошибку переноса
        pass
    _model = m
    _notice("Модель загружена")


def _iter_image_paths_from_inputs(inputs: List[Dict[str, Any]]) -> List[Path]:
    """
    Собирает пути изображений из inputs (file|directory). Для директорий — перебирает популярные расширения.
    """
    paths: List[Path] = []
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
    for it in inputs:
        typ = str(it.get("type"))
        p = Path(str(it.get("path", ""))).expanduser()
        if typ == "file" and p.is_file():
            paths.append(p)
        elif typ == "directory" and p.is_dir():
            for child in p.iterdir():
                if child.is_file() and child.suffix.lower() in exts:
                    paths.append(child)
    return paths


def _save_plotted(numpy_bgr, out_path: Path) -> None:
    """
    Сохраняет numpy-изображение (BGR) как PNG.
    """
    import numpy as np
    from PIL import Image

    # BGR -> RGB
    rgb = numpy_bgr[:, :, ::-1]
    im = Image.fromarray(rgb)
    im.save(str(out_path), format="PNG")


def pcontext_request(
    inputs: List[Dict[str, Any]], params: Dict[str, Any], ctx: Dict[str, Any]
) -> List[Dict[str, str]]:
    """
    Основной обработчик сервиса: принимает список изображений/папок, возвращает аннотированные изображения.
    Возвращает [{"image": "/path/to/annotated.png"}, ...]
    """
    global _model, _conf, _iou, _imgsz, _device, _half

    if _model is None:
        raise RuntimeError("Модель YOLO не загружена (сервис не инициализирован)")

    imgs = _iter_image_paths_from_inputs(inputs)
    if not imgs:
        raise RuntimeError("Не найдено изображений для обработки")

    out_dir = _ensure_out_dir(ctx)
    results: List[Dict[str, str]] = []

    for p in imgs:
        _notice(f"Обработка: {p.name}")
        # Вызов инференса. В новых ultralytics многие опции можно передать прямо в вызове.
        # half/device могут учитываться при .to() и/или при вызове (версии различаются).
        res_list = _model(
            source=str(p),
            conf=_conf,
            iou=_iou,
            imgsz=_imgsz,
            half=_half,
        )
        res = res_list[0]
        plotted = res.plot()  # numpy в BGR
        out_path = out_dir / f"det_{p.stem}.png"
        _save_plotted(plotted, out_path)
        results.append({"image": str(out_path)})

    return results


def pcontext_shutdown(ctx: Dict[str, Any]) -> None:
    """
    Освобождение ресурсов (в т.ч. очистка GPU-памяти при необходимости).
    """
    global _model
    try:
        # Для CUDA можно попытаться освободить память
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    _model = None
    _notice("Модель выгружена")
