from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .config import EnvMode, cache_dir
from .envs import EnvHandle, ensure_environment
from .errors import CancelledError, PContextError, TimeoutExceeded
from .logs import open_run_log, summarize_result_for_log
from .params import coerce_all_params
from .types import (
    Input,
    ResultLike,
    RunStatus,
    ScriptMeta,
    is_result_mapping,
    summarize_inputs,
)
from .utils import generate_run_id


# Коллбэки прогресса и уведомлений (опциональны)
ProgressCallback = Optional[Callable[[float], None]]
NoticeCallback = Optional[Callable[[str], None]]
WarnCallback = Optional[Callable[[str], None]]
OutputCallback = Optional[Callable[[str], None]]


@dataclass
class OneShotRunResult:
    script_id: str
    run_id: str
    status: RunStatus
    elapsed_seconds: float
    result: ResultLike
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    traceback: Optional[str] = None
    log_file: Optional[Path] = None
    env_handle: Optional[EnvHandle] = None


@dataclass
class RunOptions:
    """
    Доп. опции для запуска one-shot.
    """

    env_mode: EnvMode = EnvMode.CACHED
    timeout_seconds: Optional[float] = (
        None  # если None — берем из meta.timeout.one_shot_seconds
    )
    cwd: Optional[Path] = None
    auto_open_result: Optional[bool] = (
        None  # пока не используется тут, обработка в UI/вне
    )
    on_progress: ProgressCallback = None
    on_notice: NoticeCallback = None
    on_warn: WarnCallback = None
    on_output: OutputCallback = None


# -----------------------------
# Вспомогательные функции
# -----------------------------


def _inputs_to_jsonable(inputs: Sequence[Input]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i in inputs:
        out.append(
            {
                "type": i.kind.value,
                "path": str(i.path) if i.path else None,
                "name": i.name,
                "mime": i.mime,
                "size": i.size,
                "created": i.created_ts,
                "modified": i.modified_ts,
            }
        )
    return out


def _creation_flags() -> int:
    if os.name == "nt":
        # скрываем консольное окно
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _worker_script_path() -> Path:
    # pcontext/core/runner.py -> pcontext/workers/python_worker.py
    return Path(__file__).resolve().parent.parent / "workers" / "python_worker.py"


class _ReaderThread(threading.Thread):
    """
    Отдельный поток чтения stdout воркера для построчной обработки.
    """

    def __init__(self, pipe, on_line: Callable[[str], None]) -> None:
        super().__init__(daemon=True)
        self.pipe = pipe
        self.on_line = on_line
        self._stop = threading.Event()

    def run(self) -> None:  # type: ignore[override]
        try:
            for raw in self.pipe:  # text=True в Popen гарантирует str
                if self._stop.is_set():
                    break
                line = raw.rstrip("\r\n")
                self.on_line(line)
        except Exception:
            # Игнор: при резком завершении процесса pipe может «плюнуть» исключением
            pass

    def stop(self) -> None:
        self._stop.set()


def _terminate_tree(proc: subprocess.Popen) -> None:
    """
    Пытается корректно завершить процесс и его потомков.
    Использует psutil, если доступен. Иначе — terminate/kill.
    """
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None  # type: ignore

    if psutil is not None:
        try:
            p = psutil.Process(proc.pid)  # type: ignore[attr-defined]
            children = p.children(recursive=True)
            for ch in children:
                try:
                    ch.terminate()
                except Exception:
                    pass
            try:
                p.terminate()
            except Exception:
                pass
            gone, alive = psutil.wait_procs(children + [p], timeout=3.0)  # type: ignore[operator]
            for a in alive:
                try:
                    a.kill()
                except Exception:
                    pass
            return
        except Exception:
            # Fallback на стандартный метод
            pass

    # Fallback без psutil
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _parse_prefixed(line: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Разбирает строки вида:
      PCTX:PROGRESS <float>
      PCTX:NOTICE <text>
      PCTX:WARN <text>
      PCTX:RESULT <json>
      PCTX:EXC <json>
    Возвращает (tag, payload_str) или (None, None)
    """
    if not line.startswith("PCTX:"):
        return None, None
    # Отделим тег и полезную часть
    parts = line.split(" ", 1)
    if not parts:
        return None, None
    tag = parts[0][5:].strip()  # без "PCTX:"
    payload = parts[1] if len(parts) > 1 else ""
    return tag, payload


# -----------------------------
# Основной API: запуск one-shot
# -----------------------------


def run_one_shot(
    meta: ScriptMeta,
    inputs: Sequence[Input],
    params_overrides: Optional[Mapping[str, Any]] = None,
    options: Optional[RunOptions] = None,
) -> OneShotRunResult:
    """
    Запускает одноразовый скрипт в отдельном процессе (через python_worker).
    - Обеспечивает окружение (venv) и зависимости.
    - Собирает stdout/stderr в лог с префиксами.
    - Поддерживает таймаут и мягкое завершение.
    Возвращает OneShotRunResult со статусом и результатом/ошибкой.
    """
    opts = options or RunOptions()
    timeout = (
        opts.timeout_seconds
        if opts.timeout_seconds is not None
        else (meta.timeout.one_shot_seconds or None)
    )
    run_id = generate_run_id()

    # Готовим параметры (с приведением типов/валидацией)
    params_values = coerce_all_params(meta, params_overrides)

    # Подготовим временные каталоги и payload
    base_tmp_dir = cache_dir() / "runs" / meta.stable_id / run_id
    base_tmp_dir.mkdir(parents=True, exist_ok=True)
    cancel_flag_path = base_tmp_dir / "cancel.flag"

    ctx: Dict[str, Any] = {
        "run_id": run_id,
        "os": ("windows" if os.name == "nt" else "linux"),
        "user": _safe_username(),
        "cwd": str(opts.cwd) if opts.cwd else None,
        "tmp_dir": str(base_tmp_dir),
        "cache_dir": str(cache_dir()),
        "log_file": None,  # заполним ниже
        "cancel_flag_path": str(cancel_flag_path),
    }

    # Лог запуска
    rl = open_run_log(meta, run_id, summarize_inputs(inputs), params_values)
    ctx["log_file"] = str(rl.path) if rl.path else None

    # 1) Окружение (venv) и зависимости
    def env_log(line: str) -> None:
        rl.log_out(f"[env] {line}")
        if opts.on_output:
            opts.on_output(line)

    env_handle = ensure_environment(meta, opts.env_mode, on_output=env_log)

    # 2) Подготовка payload
    payload = {
        "script_path": str(meta.file_path),
        "entry": "pcontext_run",
        "inputs": _inputs_to_jsonable(inputs),
        "params": params_values,
        "ctx": ctx,
    }
    payload_path = base_tmp_dir / "payload.json"
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 3) Запуск воркера
    worker = _worker_script_path()
    python_exe = env_handle.python

    cmd = [str(python_exe), str(worker), "--payload", str(payload_path)]
    creationflags = _creation_flags()

    start_ts = time.time()
    result_obj: ResultLike = None
    err_type: Optional[str] = None
    err_msg: Optional[str] = None
    err_tb: Optional[str] = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            cwd=str(opts.cwd) if opts.cwd else None,
            creationflags=creationflags,
        )
        assert proc.stdout is not None

        def on_line(line: str) -> None:
            nonlocal result_obj, err_type, err_msg, err_tb
            tag, payload = _parse_prefixed(line)
            if tag is None:
                rl.log_out(line)
                if opts.on_output:
                    opts.on_output(line)
                return
            # Разбор префиксов
            if tag == "PROGRESS":
                try:
                    val = float(payload.strip())
                except Exception:
                    val = 0.0
                rl.log_out(f"[progress] {val:.3f}")
                if opts.on_progress:
                    opts.on_progress(val)
            elif tag == "NOTICE":
                rl.log_out(f"[notice] {payload}")
                if opts.on_notice:
                    opts.on_notice(payload)
            elif tag == "WARN":
                rl.log_err(f"[warn] {payload}")
                if opts.on_warn:
                    opts.on_warn(payload)
            elif tag == "RESULT":
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict) and obj.get("ok") is True:
                        result_obj = obj.get("result", None)
                    else:
                        # Если формат неожидан, логируем, но не падаем
                        rl.log_err("[result] unexpected payload format")
                except Exception as e:
                    rl.log_err(f"[result] failed to parse JSON: {e}")
            elif tag == "EXC":
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict) and obj.get("ok") is False:
                        err_type = str(obj.get("error_type") or "Error")
                        err_msg = str(obj.get("error_message") or "")
                        err_tb = str(obj.get("traceback") or "")
                except Exception as e:
                    rl.log_err(f"[exc] failed to parse JSON: {e}")
            else:
                # Неизвестный тег — просто логируем
                rl.log_out(line)

        reader = _ReaderThread(proc.stdout, on_line)
        reader.start()

        # Ожидание с таймаутом
        rc: Optional[int] = None
        while True:
            try:
                rc = proc.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                rc = None
            # Проверка таймаута
            if timeout is not None and (time.time() - start_ts) > timeout:
                rl.log_err(f"[timeout] exceeded {timeout:.1f}s, terminating...")
                _terminate_tree(proc)
                raise TimeoutExceeded(f"Превышен таймаут выполнения ({timeout:.1f}s)")
            # Проверка флага отмены
            if cancel_flag_path.exists():
                rl.log_err("[cancel] cancel flag detected, terminating...")
                _terminate_tree(proc)
                raise CancelledError("Выполнение отменено")
            if rc is not None:
                break
        # Завершение чтения
        reader.stop()
        reader.join(timeout=1.0)

        elapsed = time.time() - start_ts

        # Определение статуса
        if err_type:
            status = RunStatus.ERROR
            result_summary = None
            error_brief = f"{err_type}: {err_msg}" if err_msg else err_type
        else:
            # Если код возврата не 0, но исключения не было захвачено worker'ом — считаем ошибкой
            if rc != 0:
                status = RunStatus.ERROR
                error_brief = f"Process exited with code {rc}"
                result_summary = None
            else:
                status = RunStatus.OK
                result_summary = summarize_result_for_log(result_obj)
                error_brief = None

        rl.finalize(
            status=status,
            elapsed_seconds=elapsed,
            result_summary=result_summary,
            error_brief=error_brief,
        )
        rl.close()

        return OneShotRunResult(
            script_id=meta.stable_id,
            run_id=run_id,
            status=status,
            elapsed_seconds=elapsed,
            result=result_obj,
            error_type=err_type,
            error_message=err_msg,
            traceback=err_tb,
            log_file=rl.path,
            env_handle=env_handle,
        )

    except TimeoutExceeded as te:
        elapsed = time.time() - start_ts
        rl.finalize(
            status=RunStatus.TIMEOUT,
            elapsed_seconds=elapsed,
            result_summary=None,
            error_brief=str(te),
        )
        rl.close()
        return OneShotRunResult(
            script_id=meta.stable_id,
            run_id=run_id,
            status=RunStatus.TIMEOUT,
            elapsed_seconds=elapsed,
            result=None,
            error_type="TimeoutExceeded",
            error_message=str(te),
            traceback=None,
            log_file=rl.path,
            env_handle=env_handle,
        )
    except CancelledError as ce:
        elapsed = time.time() - start_ts
        rl.finalize(
            status=RunStatus.CANCELLED,
            elapsed_seconds=elapsed,
            result_summary=None,
            error_brief=str(ce),
        )
        rl.close()
        return OneShotRunResult(
            script_id=meta.stable_id,
            run_id=run_id,
            status=RunStatus.CANCELLED,
            elapsed_seconds=elapsed,
            result=None,
            error_type="Cancelled",
            error_message=str(ce),
            traceback=None,
            log_file=rl.path,
            env_handle=env_handle,
        )
    except BaseException as e:
        # Непредвиденная ошибка раннера
        elapsed = time.time() - start_ts
        msg = f"{e.__class__.__name__}: {e}"
        try:
            rl.finalize(
                status=RunStatus.ERROR,
                elapsed_seconds=elapsed,
                result_summary=None,
                error_brief=msg,
            )
            rl.close()
        except Exception:
            pass
        return OneShotRunResult(
            script_id=meta.stable_id,
            run_id=run_id,
            status=RunStatus.ERROR,
            elapsed_seconds=elapsed,
            result=None,
            error_type=e.__class__.__name__,
            error_message=str(e),
            traceback=None,
            log_file=rl.path if rl else None,  # type: ignore[arg-type]
            env_handle=env_handle if "env_handle" in locals() else None,  # type: ignore[truthy-bool]
        )


def _safe_username() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return "user"
