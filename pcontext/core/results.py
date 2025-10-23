from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..os_integration.common.clipboard import set_text as clipboard_set_text
from ..os_integration.common.shell_open import open_any, open_path, open_url
from .types import ALLOWED_RESULT_KEYS, ResultLike, is_result_mapping, is_url


@dataclass
class ResultAction:
    """
    Одна планируемая/выполненная операция по обработке результата.
    kind:
      - clipboard_copy
      - open_url
      - open_path
      - open_any
      - noop
    """

    kind: str
    target: Optional[str] = None
    ok: Optional[bool] = None
    message: Optional[str] = None


@dataclass
class ResultHandling:
    """
    Итог обработки результата.
    """

    planned: List[ResultAction]
    executed: bool
    succeeded: bool


def _action(
    kind: str, target: Optional[str], message: Optional[str] = None
) -> ResultAction:
    return ResultAction(kind=kind, target=target, ok=None, message=message)


def _plan_for_mapping_item(key: str, value: str) -> List[ResultAction]:
    """
    Планирует действия для пар вида {"image": "/path"}, {"link": "https://..."} и т.п.
    """
    k = key.lower().strip()
    v = str(value)

    # Поддерживаем заданный набор ключей, остальное трактуем как "any".
    if k == "link":
        return [_action("open_url", v)]
    if k in ("folder",):
        return [_action("open_path", v)]
    if k in (
        "image",
        "video",
        "audio",
        "textfile",
        "pdf",
        "doc",
        "ppt",
        "xls",
        "archive",
    ):
        return [_action("open_path", v)]
    if k == "any":
        # Может быть как путь, так и URL
        return [_action("open_any", v)]
    # Неизвестный ключ — безопасный фолбэк
    return [_action("open_any", v)]


def _plan_actions(result: ResultLike) -> List[ResultAction]:
    """
    Строит список действий для результата без их выполнения.
    """
    actions: List[ResultAction] = []
    if result is None:
        return actions

    if isinstance(result, str):
        if is_url(result):
            actions.append(_action("open_url", result))
        else:
            # Строка — трактуем как текст: копируем в буфер
            actions.append(
                _action("clipboard_copy", result, message="copy text to clipboard")
            )
        return actions

    if is_result_mapping(result):  # type: ignore[arg-type]
        for k, v in dict(result).items():  # type: ignore[union-attr]
            actions.extend(_plan_for_mapping_item(k, v))
        return actions

    if isinstance(result, (list, tuple)):
        for it in result:
            if isinstance(it, str):
                if is_url(it):
                    actions.append(_action("open_url", it))
                else:
                    actions.append(
                        _action("clipboard_copy", it, message="copy text to clipboard")
                    )
            elif is_result_mapping(it):  # type: ignore[arg-type]
                for k, v in dict(it).items():  # type: ignore[union-attr]
                    actions.extend(_plan_for_mapping_item(k, v))
            else:
                # Неизвестный элемент — строковое представление в буфер
                actions.append(
                    _action("clipboard_copy", str(it), message="copy text to clipboard")
                )
        return actions

    # Все остальное — строковое представление в буфер
    actions.append(
        _action("clipboard_copy", str(result), message="copy text to clipboard")
    )
    return actions


def _execute_action(act: ResultAction) -> ResultAction:
    """
    Выполняет одно действие и проставляет флаг ok.
    """
    ok = False
    try:
        if act.kind == "clipboard_copy":
            ok = clipboard_set_text(act.target or "")
        elif act.kind == "open_url":
            ok = open_url(act.target or "")
        elif act.kind == "open_path":
            ok = open_path(act.target or "")
        elif act.kind == "open_any":
            ok = open_any(act.target or "")
        elif act.kind == "noop":
            ok = True
        else:
            ok = False
    except Exception:
        ok = False
    act.ok = ok
    return act


def handle_result(
    result: ResultLike,
    auto_open: bool = True,
) -> ResultHandling:
    """
    Главная функция: планирует и (опционально) выполняет действия по результату.
    - result: результат из pcontext_run/pcontext_request
    - auto_open: если True — выполняем действия (копируем текст/открываем файлы/ссылки).
                 если False — только возвращаем план действий (executed=False).
    Возвращает ResultHandling.
    """
    actions = _plan_actions(result)

    if not auto_open:
        return ResultHandling(planned=actions, executed=False, succeeded=True)

    executed: List[ResultAction] = []
    all_ok = True
    for act in actions:
        executed.append(_execute_action(act))
        if act.ok is False:
            all_ok = False

    return ResultHandling(planned=executed, executed=True, succeeded=all_ok)


__all__ = [
    "ResultAction",
    "ResultHandling",
    "handle_result",
]
