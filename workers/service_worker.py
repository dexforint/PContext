from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

READY_PREFIX = "PCTX:READY "
RESP_PREFIX = "PCTX:RESP "
EXC_PREFIX = "PCTX:EXC "
BYE_PREFIX = "PCTX:BYE "


def _print_ready(info: Dict[str, Any]) -> None:
    try:
        text = json.dumps({"ok": True, **info}, ensure_ascii=False)
    except Exception:
        text = '{"ok": true}'
    print(READY_PREFIX + text, flush=True)


def _print_resp(req_id: Optional[str], obj: Any) -> None:
    try:
        payload = {"ok": True, "req_id": req_id, "result": _jsonify_result(obj)}
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        payload = {"ok": True, "req_id": req_id, "result": {"any": repr(obj)}}
        text = json.dumps(payload, ensure_ascii=False)
    print(RESP_PREFIX + text, flush=True)


def _print_exc(req_id: Optional[str], exc: BaseException) -> None:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    payload = {
        "ok": False,
        "req_id": req_id,
        "error_type": exc.__class__.__name__,
        "error_message": str(exc),
        "traceback": tb,
    }
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = json.dumps(payload, ensure_ascii=True)
    print(EXC_PREFIX + text, flush=True)


def _print_bye(info: Dict[str, Any]) -> None:
    try:
        text = json.dumps({"ok": True, **info}, ensure_ascii=False)
    except Exception:
        text = '{"ok": true}'
    print(BYE_PREFIX + text, flush=True)


def _jsonify_result(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        out: Dict[str, str] = {}
        for k, v in obj.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return repr(obj)
            out[k] = v
        return out
    if isinstance(obj, (list, tuple)):
        out_list: List[Any] = []
        for it in obj:
            if isinstance(it, str):
                out_list.append(it)
            elif isinstance(it, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in it.items()
            ):
                out_list.append({str(k): str(v) for k, v in it.items()})
            else:
                out_list.append(repr(it))
        return out_list
    return repr(obj)


def _import_module_from_path(
    script_path: Path, module_name: Optional[str] = None
) -> ModuleType:
    mname = module_name or f"pctx_svc_{abs(hash(str(script_path))) % (10**8)}"
    spec = importlib.util.spec_from_file_location(mname, str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось создать spec для модуля {script_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mname] = mod
    script_dir = str(script_path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _apply_cwd(cwd: Optional[str]) -> None:
    if not cwd:
        return
    try:
        os.chdir(cwd)
    except Exception:
        pass


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="PContext Service Worker")
    ap.add_argument(
        "--payload",
        required=True,
        help="Путь к JSON-файлу с данными запуска сервиса (script_path, entries, init_params, ctx)",
    )
    return ap.parse_args(argv)


def _load_payload(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("payload должен быть JSON-объектом")
    return data


def _read_stdin_line() -> Optional[str]:
    try:
        line = sys.stdin.readline()
        if line == "":
            return None  # EOF
        return line.rstrip("\r\n")
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ns = _parse_args(argv)
    try:
        payload = _load_payload(ns.payload)
    except Exception as e:
        _print_exc(None, e)
        return 2

    script_path = Path(payload.get("script_path", ""))
    entries = payload.get("entries", {}) or {}
    init_entry = str(entries.get("init", "pcontext_init"))
    request_entry_default = str(entries.get("request", "pcontext_request"))
    shutdown_entry = str(entries.get("shutdown", "pcontext_shutdown"))

    init_params = payload.get("init_params", {}) or {}
    ctx = payload.get("ctx", {}) or {}
    cwd = ctx.get("cwd")

    try:
        if not script_path.exists():
            raise FileNotFoundError(f"Скрипт не найден: {script_path}")
        _apply_cwd(cwd)
        mod = _import_module_from_path(script_path)

        # init (опционально)
        init_func = getattr(mod, init_entry, None)
        if callable(init_func):
            try:
                init_func(init_params, ctx)
            except BaseException as e:
                _print_exc(None, e)
                return 1

        # Базовая функция по умолчанию
        default_req_func = getattr(mod, request_entry_default, None)
        if default_req_func is None or not callable(default_req_func):
            raise AttributeError(
                f"В модуле '{script_path.name}' не найдена функция {request_entry_default}(inputs, params, ctx)"
            )

        shutdown_func = getattr(mod, shutdown_entry, None)
        _print_ready({"entry": request_entry_default})

        # Главный цикл: читаем команды JSON по строкам из stdin
        while True:
            line = _read_stdin_line()
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except Exception as e:
                _print_exc(None, e)
                continue
            if not isinstance(cmd, dict):
                _print_exc(None, ValueError("Команда должна быть JSON-объектом"))
                continue

            op = str(cmd.get("op") or "")
            req_id = cmd.get("req_id")
            if op == "request":
                inputs = cmd.get("inputs", []) or []
                params = cmd.get("params", {}) or {}
                req_ctx = cmd.get("ctx", None)

                # Поддержка явного указания entry на запрос
                req_entry = cmd.get("entry")
                if isinstance(req_entry, str) and req_entry:
                    func = getattr(mod, req_entry, None)
                    if not callable(func):
                        _print_exc(
                            req_id,
                            AttributeError(
                                f"В модуле '{script_path.name}' нет функции '{req_entry}'"
                            ),
                        )
                        continue
                    target_func = func
                else:
                    target_func = default_req_func

                try:
                    result = target_func(
                        inputs, params, req_ctx if req_ctx is not None else ctx
                    )
                    _print_resp(req_id, result)
                except BaseException as e:
                    _print_exc(req_id, e)

            elif op == "ping":
                _print_resp(req_id, "pong")
            elif op == "shutdown":
                if callable(shutdown_func):
                    try:
                        shutdown_func(ctx)
                    except BaseException as e:
                        _print_exc(req_id, e)
                _print_bye({"reason": "shutdown"})
                return 0
            else:
                _print_exc(req_id, ValueError(f"Неизвестная операция: {op!r}"))

        if callable(shutdown_func):
            try:
                shutdown_func(ctx)
            except BaseException as e:
                _print_exc(None, e)
        _print_bye({"reason": "eof"})
        return 0

    except SystemExit as se:
        code = se.code if isinstance(se.code, int) else 1
        _print_exc(None, se)
        return int(code)
    except BaseException as e:
        _print_exc(None, e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
