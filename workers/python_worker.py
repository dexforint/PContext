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

# Префиксы служебных сообщений, которые Runner будет парсить
RESULT_PREFIX = "PCTX:RESULT "
EXC_PREFIX = "PCTX:EXC "


def _print_result(obj: Any) -> None:
    """
    Печатает финальный JSON-результат с префиксом.
    """
    try:
        payload = {"ok": True, "result": _jsonify_result(obj)}
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        # На случай, если результат не сериализуется — отправим repr
        payload = {"ok": True, "result": {"any": repr(obj)}}
        text = json.dumps(payload, ensure_ascii=False)
    print(RESULT_PREFIX + text, flush=True)


def _print_exc(exc: BaseException) -> None:
    """
    Печатает информацию об исключении в JSON виде с префиксом.
    """
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    payload = {
        "ok": False,
        "error_type": exc.__class__.__name__,
        "error_message": str(exc),
        "traceback": tb,
    }
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        # Фолбэк на ASCII
        text = json.dumps(payload, ensure_ascii=True)
    print(EXC_PREFIX + text, flush=True)


def _jsonify_result(obj: Any) -> Any:
    """
    Приводит результат к JSON-дружелюбному виду без «импеданса несоответствия».
    Допустимые нативные виды:
      - None
      - str
      - dict[str, str]
      - list[ (строки или dict[str, str]) ]
    Всё остальное — строковое представление.
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        # Проверим, что словарь: str -> str
        out: Dict[str, str] = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                return repr(obj)
            if not isinstance(v, str):
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
    # Фолбэк — строковое представление
    return repr(obj)


def _import_module_from_path(
    script_path: Path, module_name: Optional[str] = None
) -> ModuleType:
    """
    Импортирует модуль из произвольного пути файла .py с указанным именем модуля.
    """
    mname = module_name or f"pctx_user_{abs(hash(str(script_path))) % (10**8)}"
    spec = importlib.util.spec_from_file_location(mname, str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось создать spec для модуля {script_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mname] = mod
    # Вставим каталог скрипта в sys.path для корректных относительных импортов
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
        # Не критично — продолжаем в текущем каталоге
        pass


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="PContext Python Worker")
    ap.add_argument(
        "--payload",
        required=True,
        help="Путь к JSON-файлу с данными вызова (script_path, entry, inputs, params, ctx)",
    )
    return ap.parse_args(argv)


def _load_payload(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("payload должен быть JSON-объектом")
    return data


def _call_user_function(func, entry: str, inputs, params, ctx):
    """
    Универсальный вызов пользовательской функции с «лучшим усилием» по сигнатуре:
      - pcontext_run: пробуем (inputs, params, ctx) -> (inputs, ctx) -> (inputs,)
      - pcontext_accept: пробуем (inputs, ctx) -> (inputs, params, ctx) -> (inputs,)
      - другие entry: сначала (inputs, params, ctx), затем (inputs, ctx), затем (inputs,)
    """
    # Попробуем разные варианты вызова — от самого специфичного к более общим
    tries = []
    if entry == "pcontext_accept":
        tries = [
            (inputs, ctx),
            (inputs, params, ctx),
            (inputs,),
        ]
    elif entry == "pcontext_run":
        tries = [
            (inputs, params, ctx),
            (inputs, ctx),
            (inputs,),
        ]
    else:
        tries = [
            (inputs, params, ctx),
            (inputs, ctx),
            (inputs,),
        ]
    last_err: Optional[Exception] = None
    for args in tries:
        try:
            return func(*args)
        except TypeError as te:
            last_err = te
            continue
    # Если ничего не подошло — бросим последнюю ошибку
    if last_err is not None:
        raise last_err
    # Нечего вызывать
    raise AttributeError(f"Функция '{entry}' недоступна для вызова")


def main(argv: Optional[List[str]] = None) -> int:
    # Гарантируем неблокирующий stdout
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ns = _parse_args(argv)
    try:
        payload = _load_payload(ns.payload)
    except Exception as e:
        _print_exc(e)
        return 2

    script_path = Path(payload.get("script_path", ""))
    entry = str(payload.get("entry", "pcontext_run"))
    inputs = payload.get("inputs", [])
    params = payload.get("params", {}) or {}
    ctx = payload.get("ctx", {}) or {}
    cwd = ctx.get("cwd")

    try:
        if not script_path.exists():
            raise FileNotFoundError(f"Скрипт не найден: {script_path}")
        _apply_cwd(cwd)
        mod = _import_module_from_path(script_path)
        func = getattr(mod, entry, None)
        if func is None:
            # Автоматический фолбэк для one-shot
            if entry == "pcontext_run":
                raise AttributeError(
                    f"В модуле '{script_path.name}' не найдена функция pcontext_run(inputs, params, ctx)"
                )
            raise AttributeError(
                f"В модуле '{script_path.name}' не найдена функция '{entry}'"
            )
        # Вызов с гибкой сигнатурой
        result = _call_user_function(func, entry, inputs, params, ctx)
        _print_result(result)
        return 0
    except SystemExit as se:
        # Скрипт вызвал sys.exit — обработаем как ошибку, чтобы пользователь видел детали
        try:
            code = int(getattr(se, "code", 1))
        except Exception:
            code = 1
        _print_exc(se)
        return int(code) if code is not None else 1
    except BaseException as e:
        _print_exc(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
