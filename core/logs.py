from __future__ import annotations

import io
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import ensure_app_dirs, logs_dir
from .types import ResultLike, RunStatus, ScriptMeta, is_result_mapping, is_url


# Сколько логов хранить на скрипт (последние N)
MAX_LOGS_PER_SCRIPT = 50


def _sanitize_id(s: str) -> str:
    """
    Безопасное имя для каталога/файла: только a-z0-9_-.
    """
    s = s.strip().lower().replace(" ", "_")
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s or "script"


def script_logs_dir(script_id: str) -> Path:
    """
    Каталог логов для конкретного скрипта.
    """
    ensure_app_dirs()
    d = logs_dir() / "scripts" / _sanitize_id(script_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> datetime:
    return datetime.now()


def _ts_filename(dt: datetime) -> str:
    # 2025-03-04_12-30-05
    return dt.strftime("%Y-%m-%d_%H-%M-%S")


def _fmt_human_ts(dt: datetime) -> str:
    # 2025-03-04 12:30:05
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_elapsed(seconds: float) -> str:
    # hh:mm:ss.mmm
    ms = int((seconds - int(seconds)) * 1000)
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}.{ms:03d}"


def _shorten_value(v: Any, limit: int = 200) -> str:
    """
    Компактное строковое представление значения параметра.
    Секреты маскируются заранее, здесь просто обрезаем.
    """
    try:
        if isinstance(v, (str, Path)):
            s = str(v)
        elif isinstance(v, (int, float, bool)) or v is None:
            s = repr(v)
        elif isinstance(v, (list, tuple, set)):
            s = f"{type(v).__name__}[{len(v)}]"
        elif isinstance(v, dict):
            s = f"dict[{len(v)}]"
        else:
            s = repr(v)
    except Exception:
        s = "<unrepr>"
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def summarize_result_for_log(result: ResultLike) -> str:
    """
    Короткое описание результата для нижней части лога.
    """
    if result is None:
        return "result: None"
    if isinstance(result, str):
        if is_url(result):
            return f"result: link -> {result}"
        # Иначе — текст (мы кладем его в буфер, но в лог сохраняем только факт)
        return f"result: text (len={len(result)})"
    if is_result_mapping(result):  # type: ignore[arg-type]
        items = [f"{k} -> {v}" for k, v in dict(result).items()]  # type: ignore[union-attr]
        return "result: {" + ", ".join(items) + "}"
    if isinstance(result, (list, tuple)):
        parts: List[str] = []
        for idx, it in enumerate(result):
            if isinstance(it, str):
                if is_url(it):
                    parts.append(f"[{idx}]=link")
                else:
                    parts.append(f"[{idx}]=text(len={len(it)})")
            elif is_result_mapping(it):  # type: ignore[arg-type]
                parts.append(f"[{idx}]=map({','.join(list(it.keys()))})")  # type: ignore[union-attr]
            else:
                parts.append(f"[{idx}]={type(it).__name__}")
        return "result: sequence[" + ", ".join(parts) + "]"
    return f"result: {type(result).__name__}"


def rotate_logs(script_id: str, keep_latest: int = MAX_LOGS_PER_SCRIPT) -> int:
    """
    Хранит только N последних логов на скрипт, удаляя старые.
    Возвращает количество удаленных файлов.
    """
    d = script_logs_dir(script_id)
    files = [p for p in d.glob("*.log") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for p in files[keep_latest:]:
        try:
            p.unlink(missing_ok=True)
            removed += 1
        except Exception:
            # Игнорируем ошибки удаления
            pass
    return removed


class RunLog:
    """
    Объект для ведения лога одного запуска.
    Использование:
      rl = open_run_log(meta, run_id, inputs_summary, params)
      rl.log_out("line")
      ...
      rl.finalize(RunStatus.OK, elapsed, result_summary="...")
      rl.close()
    """

    def __init__(
        self,
        script_id: str,
        script_name: str,
        script_path: Optional[Path],
        version_sig: Optional[str],
        run_id: str,
        start_dt: Optional[datetime] = None,
    ) -> None:
        self.script_id = script_id
        self.script_name = script_name
        self.script_path = Path(script_path) if script_path else None
        self.version_sig = version_sig
        self.run_id = run_id
        self.start_dt = start_dt or _now()
        self._fh: Optional[io.TextIOWrapper] = None
        self.path: Optional[Path] = None
        self._closed = False

    def _ensure_open(self) -> None:
        if self._fh:
            return
        d = script_logs_dir(self.script_id)
        fname = f"{_ts_filename(self.start_dt)}__{_sanitize_id(self.run_id)}.log"
        self.path = d / fname
        self._fh = self.path.open("w", encoding="utf-8", newline="\n")

    def _writeln(self, line: str = "") -> None:
        self._ensure_open()
        assert self._fh is not None
        # Нормализуем переводы строк
        line = line.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" in line:
            self._fh.write(line)
            if not line.endswith("\n"):
                self._fh.write("\n")
        else:
            self._fh.write(line + "\n")
        self._fh.flush()

    def write_header(
        self,
        inputs_summary: str,
        params: Dict[str, Any],
        secret_mask_map: Optional[Dict[str, bool]] = None,
    ) -> None:
        """
        Записывает шапку лога: скрипт, версия, время, входы, параметры.
        secret_mask_map: имя->True если нужно скрыть значение.
        """
        self._ensure_open()
        self._writeln("=== PContext Run Log ===")
        self._writeln(f"Start:  {_fmt_human_ts(self.start_dt)}")
        self._writeln(f"Script: {self.script_name}  (id={self.script_id})")
        if self.script_path:
            self._writeln(f"Path:   {self.script_path}")
        self._writeln(f"Run ID: {self.run_id}")
        if self.version_sig:
            self._writeln(f"Version:{self.version_sig}")
        self._writeln(f"Inputs: {inputs_summary}")
        if params:
            self._writeln("Params:")
            for k in sorted(params.keys()):
                v = params.get(k, None)
                masked = secret_mask_map.get(k, False) if secret_mask_map else False
                shown = "******" if masked else _shorten_value(v)
                self._writeln(f"  - {k}: {shown}")
        self._writeln("-" * 40)

    def log_out(self, line: str) -> None:
        t = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._writeln(f"{t} [OUT] {line}")

    def log_err(self, line: str) -> None:
        t = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._writeln(f"{t} [ERR] {line}")

    def finalize(
        self,
        status: RunStatus,
        elapsed_seconds: float,
        result_summary: Optional[str] = None,
        error_brief: Optional[str] = None,
    ) -> None:
        """
        Записывает итог и выполняет ротацию логов.
        """
        self._writeln("-" * 40)
        self._writeln(f"Finish: {_fmt_human_ts(_now())}")
        self._writeln(f"Elapsed: {_fmt_elapsed(elapsed_seconds)}")
        self._writeln(f"Status: {status.value}")
        if error_brief:
            self._writeln(f"Error:  {error_brief}")
        if result_summary:
            self._writeln(result_summary)
        self._writeln("=" * 28)
        # Ротация
        rotate_logs(self.script_id, keep_latest=MAX_LOGS_PER_SCRIPT)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._fh:
                self._fh.flush()
                self._fh.close()
        finally:
            self._fh = None


def open_run_log(
    meta: ScriptMeta,
    run_id: str,
    inputs_summary: str,
    params_values: Dict[str, Any],
) -> RunLog:
    """
    Создает и подготавливает RunLog, записывает шапку с учетом секретных параметров.
    """
    rl = RunLog(
        script_id=meta.stable_id,
        script_name=meta.name,
        script_path=meta.file_path,
        version_sig=meta.version_sig,
        run_id=run_id,
    )
    # Карта секретов по имени
    secret_map: Dict[str, bool] = {}
    for pname, pspec in (meta.params or {}).items():
        secret_map[pname] = bool(getattr(pspec, "secret", False))
    rl.write_header(inputs_summary, params_values, secret_mask_map=secret_map)
    return rl


__all__ = [
    "MAX_LOGS_PER_SCRIPT",
    "script_logs_dir",
    "rotate_logs",
    "summarize_result_for_log",
    "RunLog",
    "open_run_log",
]
