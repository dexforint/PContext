"""
---
name: "YOLO v8 — сервис (HTTP)"
type: "one-shot"
description: "Запускает/останавливает локальный HTTP-сервис с моделью YOLO v8 для детекции объектов."
group: "Vision/YOLO"
accepts:
  scope: "background"
depends:
  pip: ["ultralytics>=8.0", "fastapi>=0.103", "uvicorn>=0.22", "pillow>=9.5"]
params:
  action:
    type: "enum"
    title: "Действие"
    options: ["start", "stop", "status"]
    default: "start"
  host:
    type: "str"
    title: "Host"
    default: "127.0.0.1"
  port:
    type: "int"
    title: "Port"
    default: 5005
  device:
    type: "str"
    title: "Устройство"
    default: "cpu"
    description: "Например: cpu, cuda:0"
  idle_ttl_seconds:
    type: "int"
    title: "Автозавершение при простое (сек)"
    default: 600
auto_open_result: false
---
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

# ============
# Пользовательский API PContext
# ============


def pcontext_run(inputs, params, ctx):
    """
    Запускает/останавливает/показывает статус YOLO-HTTP сервиса.
    Возвращает текстовую строку со статусом (скопируется в буфер обмена PContext).
    """
    host = str(params.get("host") or "127.0.0.1").strip()
    port = int(params.get("port") or 5005)
    action = str(params.get("action") or "start").strip().lower()
    device = str(params.get("device") or "cpu").strip()
    idle = int(params.get("idle_ttl_seconds") or 600)

    # Где хранить PID/метаданные — используем кэш каталога PContext из ctx
    base_dir = (
        Path(ctx.get("cache_dir") or Path.home() / ".cache") / "pcontext" / "yolo_http"
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    pid_path = base_dir / "server.pid"
    meta_path = base_dir / "server.json"

    if action == "status":
        if _server_alive(pid_path):
            return f"YOLO сервер запущен (PID {pid_path.read_text().strip()}) на http://{host}:{port}"
        return "YOLO сервер не запущен"

    if action == "stop":
        if not pid_path.exists():
            return "YOLO сервер не запущен"
        ok = _stop_server(pid_path)
        return "YOLO сервер остановлен" if ok else "Не удалось остановить YOLO сервер"

    # action == start
    if _server_alive(pid_path):
        return f"YOLO сервер уже запущен (PID {pid_path.read_text().strip()})"

    # Запускаем сервер в фоновом процессе: python <этот_файл> --serve ...
    this_script = Path(__file__).resolve()
    py = sys.executable

    cmd = [
        py,
        str(this_script),
        "--serve",
        "--host",
        host,
        "--port",
        str(port),
        "--device",
        device,
        "--idle-ttl",
        str(idle),
        "--pid-file",
        str(pid_path),
    ]

    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = (
            CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
        )
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
        kwargs["close_fds"] = True

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
        meta = {
            "host": host,
            "port": port,
            "device": device,
            "idle_ttl_seconds": idle,
            "started_ts": int(time.time()),
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"YOLO сервер запускается на http://{host}:{port}"
    except Exception as e:
        return f"Ошибка старта сервера: {e}"


# ============
# Внутренние утилиты
# ============


def _server_alive(pid_path: Path) -> bool:
    try:
        if not pid_path.exists():
            return False
        pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
        if pid <= 0:
            return False
        # Проверка наличия процесса
        if os.name == "nt":
            try:
                import psutil  # type: ignore

                return psutil.pid_exists(pid) and psutil.Process(pid).is_running()  # type: ignore[attr-defined]
            except Exception:
                # Фолбэк: считаем, что жив (нет надёжной проверки без psutil)
                return True
        else:
            try:
                os.kill(pid, 0)  # type: ignore[arg-type]
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
    except Exception:
        return False


def _stop_server(pid_path: Path) -> bool:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        pid = 0
    try:
        if pid > 0:
            if os.name == "nt":
                try:
                    import psutil  # type: ignore

                    p = psutil.Process(pid)  # type: ignore[attr-defined]
                    p.terminate()
                    try:
                        p.wait(timeout=2.0)
                    except Exception:
                        p.kill()
                except Exception:
                    # taskkill
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"], check=False
                    )
            else:
                os.kill(pid, signal.SIGTERM)  # type: ignore[arg-type]
        pid_path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


# ============
# Режим сервера (FastAPI + Uvicorn)
# ============


def _parse_srv_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--idle-ttl", type=int, default=600)
    ap.add_argument("--pid-file", required=True)
    return ap.parse_args(argv)


def _run_server(
    host: str, port: int, device: str, idle_ttl: int, pid_file: str
) -> None:
    # Ленивая загрузка зависимостей
    from fastapi import FastAPI, UploadFile, File
    from fastapi.responses import Response, JSONResponse
    import uvicorn
    from ultralytics import YOLO
    from PIL import Image
    import numpy as np

    app = FastAPI(title="PContext YOLO Service", version="1.0")

    # PID-файл
    try:
        Path(pid_file).parent.mkdir(parents=True, exist_ok=True)
        Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    # Состояние
    state = {
        "model": None,
        "device": device,
        "last_req": time.monotonic(),
        "idle_ttl": int(idle_ttl),
    }

    def touch():
        state["last_req"] = time.monotonic()

    def idle_watcher():
        while True:
            time.sleep(2.0)
            ttl = state["idle_ttl"]
            if ttl and ttl > 0 and (time.monotonic() - state["last_req"]) > ttl:
                # Завершаем процесс
                try:
                    Path(pid_file).unlink(missing_ok=True)
                except Exception:
                    pass
                os._exit(0)

    threading.Thread(target=idle_watcher, daemon=True).start()

    @app.get("/health")
    def health():
        touch()
        return {"ok": True, "device": state["device"]}

    @app.post("/detect")
    async def detect(file: UploadFile = File(...)):
        touch()
        # Lazy load model
        if state["model"] is None:
            try:
                m = YOLO("yolov8n.pt")
                m.to(state["device"])
                state["model"] = m
            except Exception as e:
                return JSONResponse(
                    {"ok": False, "error": f"model load failed: {e}"}, status_code=500
                )
        try:
            raw = await file.read()
            img = Image.open(BytesIO(raw)).convert("RGB")
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"bad image: {e}"}, status_code=400
            )
        try:
            res = state["model"](img)[0]
            # Получим визуализацию
            plotted = res.plot()  # numpy array (BGR)
            # Преобразуем BGR->RGB
            plotted = plotted[:, :, ::-1]
            im = Image.fromarray(plotted)
            buf = BytesIO()
            im.save(buf, format="PNG")
            data = buf.getvalue()
            return Response(content=data, media_type="image/png")
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"inference failed: {e}"}, status_code=500
            )

    # Запуск uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")

    # Очистка PID-файла на выходе
    try:
        Path(pid_file).unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    # Поддержка фонового режима запуска сервера:
    # python yolo_service_http.py --serve --host 127.0.0.1 --port 5005 --device cpu --idle-ttl 600 --pid-file <path>
    if "--serve" in sys.argv:
        ns = _parse_srv_args(sys.argv[1:])
        _run_server(ns.host, int(ns.port), ns.device, int(ns.idle_ttl), ns.pid_file)
        sys.exit(0)
