from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from .config import data_dir
from .errors import ParamValidationError
from .types import ParamType, ParameterSpec, ScriptMeta


# -----------------------------
# Приведение типов и валидация
# -----------------------------

_TRUE_SET = {"1", "true", "yes", "on", "y", "t"}
_FALSE_SET = {"0", "false", "no", "off", "n", "f"}


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(int(v))
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    raise ParamValidationError(f"Ожидался bool, получено: {v!r}")


def _coerce_int(v: Any) -> int:
    if isinstance(v, bool):
        # избегаем True -> 1
        raise ParamValidationError("Ожидался int, получен bool")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if float(int(v)) == float(v):
            return int(v)
        raise ParamValidationError(f"Невозможно привести float {v!r} к int без потери")
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except Exception:
            pass
    raise ParamValidationError(f"Ожидался int, получено: {v!r}")


def _coerce_float(v: Any) -> float:
    if isinstance(v, bool):
        raise ParamValidationError("Ожидался float, получен bool")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        try:
            return float(s)
        except Exception:
            pass
    raise ParamValidationError(f"Ожидался float, получено: {v!r}")


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _ensure_regex_match(value: str, pattern: Optional[str], param_name: str) -> None:
    if not pattern:
        return
    try:
        regex = re.compile(pattern)
    except Exception as e:
        raise ParamValidationError(
            f"Неверный regex в спецификации параметра '{param_name}': {e}"
        )
    if not regex.fullmatch(value):
        raise ParamValidationError(
            f"Значение параметра '{param_name}' не соответствует шаблону {pattern!r}"
        )


def _enforce_range(
    num: float, pmin: Optional[float], pmax: Optional[float], param_name: str
) -> float:
    if pmin is not None and num < pmin:
        raise ParamValidationError(
            f"Значение параметра '{param_name}' меньше минимального ({num} < {pmin})"
        )
    if pmax is not None and num > pmax:
        raise ParamValidationError(
            f"Значение параметра '{param_name}' больше максимального ({num} > {pmax})"
        )
    return num


def _coerce_enum(v: Any, options: Optional[List[str]], param_name: str) -> str:
    s = _coerce_str(v)
    if not options:
        # Без options считаем любую строку допустимой
        return s
    # Разрешим регистронезависимое совпадение
    lower_map = {opt.lower(): opt for opt in options}
    key = s.lower()
    if key in lower_map:
        return lower_map[key]
    raise ParamValidationError(
        f"Недопустимое значение параметра '{param_name}': {s!r}. Допустимые: {', '.join(options)}"
    )


def _coerce_slider(v: Any, spec: ParameterSpec, param_name: str) -> Union[int, float]:
    # Определим «целочисленность» по шагу и мин/макс
    prefer_int = False
    if spec.step is not None:
        try:
            prefer_int = float(spec.step).is_integer()
        except Exception:
            prefer_int = False
    if spec.min is not None and spec.max is not None:
        try:
            prefer_int = (
                prefer_int
                and float(spec.min).is_integer()
                and float(spec.max).is_integer()
            )
        except Exception:
            pass
    if prefer_int:
        num = float(_coerce_int(v))
    else:
        num = _coerce_float(v)
    num = _enforce_range(num, spec.min, spec.max, param_name)
    # Если prefer_int — вернем int
    if prefer_int:
        return int(num)
    return float(num)


def _split_to_list(s: str) -> List[str]:
    # Разделители: запятая, точка с запятой, перевод строки
    parts = [p.strip() for p in re.split(r"[,\n;]+", s) if p.strip() != ""]
    return parts


def _coerce_list_str(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return _split_to_list(v)
    raise ParamValidationError(f"Ожидался список строк или строка, получено: {v!r}")


def _coerce_list_int(v: Any) -> List[int]:
    if isinstance(v, list):
        out: List[int] = []
        for x in v:
            out.append(_coerce_int(x))
        return out
    if isinstance(v, str):
        parts = _split_to_list(v)
        return [_coerce_int(p) for p in parts]
    raise ParamValidationError(f"Ожидался список чисел или строка, получено: {v!r}")


def _coerce_dict(v: Any, param_name: str) -> Dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return {}
        try:
            d = json.loads(s)
            if not isinstance(d, dict):
                raise ParamValidationError(
                    f"Строка для параметра '{param_name}' должна содержать JSON-объект ({{}})"
                )
            return d
        except json.JSONDecodeError as e:
            raise ParamValidationError(
                f"Не удалось разобрать JSON для параметра '{param_name}': {e.msg} (pos {e.pos})"
            )
    raise ParamValidationError(f"Ожидался dict или строка JSON, получено: {v!r}")


def coerce_param_value(param_name: str, spec: ParameterSpec, value: Any) -> Any:
    """
    Приводит значение к типу, указанному в спецификации параметра, и валидирует ограничения.
    """
    t = spec.type
    if t is ParamType.BOOL:
        return _coerce_bool(value)
    if t is ParamType.INT:
        num = _coerce_int(value)
        _enforce_range(float(num), spec.min, spec.max, param_name)
        return num
    if t is ParamType.FLOAT:
        numf = _coerce_float(value)
        _enforce_range(numf, spec.min, spec.max, param_name)
        return numf
    if t is ParamType.STR:
        s = _coerce_str(value)
        _ensure_regex_match(s, spec.regex, param_name)
        return s
    if t is ParamType.ENUM:
        return _coerce_enum(value, spec.options, param_name)
    if t is ParamType.SLIDER:
        return _coerce_slider(value, spec, param_name)
    if t is ParamType.TEXT:
        s = _coerce_str(value)
        _ensure_regex_match(s, spec.regex, param_name)
        return s
    if t is ParamType.FILE:
        # Просто строка пути (валидность/существование может проверяться при запуске)
        return _coerce_str(value)
    if t is ParamType.FOLDER:
        return _coerce_str(value)
    if t is ParamType.LIST_STR:
        return _coerce_list_str(value)
    if t is ParamType.LIST_INT:
        return _coerce_list_int(value)
    if t is ParamType.DICT:
        return _coerce_dict(value, param_name)
    if t is ParamType.SECRET:
        # Секрет — по сути строка; маскирование делается на уровне логов/UI
        return _coerce_str(value)
    # Неизвестный/неподдерживаемый тип
    raise ParamValidationError(f"Неподдерживаемый тип параметра '{param_name}': {t}")


def apply_defaults(meta: ScriptMeta) -> Dict[str, Any]:
    """
    Возвращает словарь параметров с дефолтными значениями из метаданных.
    """
    result: Dict[str, Any] = {}
    for name, spec in (meta.params or {}).items():
        result[name] = spec.default
    return result


def coerce_all_params(
    meta: ScriptMeta, provided: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """
    Приводит и валидирует все параметры на основе метаданных.
    Неизвестные ключи игнорируются. Отсутствующие — берутся из default.
    """
    provided = dict(provided or {})
    result = apply_defaults(meta)
    for name, spec in (meta.params or {}).items():
        if name in provided:
            try:
                result[name] = coerce_param_value(name, spec, provided[name])
            except ParamValidationError:
                # Пробрасываем дальше с уточнением имени параметра
                raise
        else:
            # Приводим и default, чтобы в рантайме быть уверенными в типе
            try:
                result[name] = coerce_param_value(name, spec, spec.default)
            except ParamValidationError as e:
                # Если default невалидный — это ошибка метаданных, но обозначим явно
                raise ParamValidationError(
                    f"Некорректный default для параметра '{name}': {e}"
                )
    return result


# -----------------------------
# Профили параметров (хранение)
# -----------------------------


def _sanitize_id(s: str) -> str:
    # безопасное имя для файлов/каталогов
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip()) or "script"


def _profiles_dir(script_id: str) -> Path:
    return data_dir() / "params" / _sanitize_id(script_id)


def _profiles_path(script_id: str) -> Path:
    d = _profiles_dir(script_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / "profiles.json"


def _jsonable(value: Any) -> Any:
    # Конвертируем значения к JSON-дружественным
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_jsonable(x) for x in value]
    if isinstance(value, tuple):
        return [_jsonable(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    # Фолбэк — строковое представление
    return str(value)


class ParamProfiles:
    """
    Простое хранилище профилей параметров для скрипта.
    Формат JSON:
    {
      "profiles": {
        "default": {...},
        "my-preset": {...}
      },
      "last_used": "my-preset"
    }
    """

    def __init__(self, script_id: str) -> None:
        self.script_id = script_id
        self.path = _profiles_path(script_id)
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._last_used: Optional[str] = None
        self.load()

    def load(self) -> None:
        p = self.path
        if not p.exists():
            self._profiles = {}
            self._last_used = None
            return
        try:
            with p.open("r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            # Поврежденный файл — начнем с пустого
            self._profiles = {}
            self._last_used = None
            return
        if not isinstance(obj, dict):
            self._profiles = {}
            self._last_used = None
            return
        profiles = obj.get("profiles") or {}
        if isinstance(profiles, dict):
            # нормализуем ключи->str и значения->dict
            self._profiles = {
                str(k): (v if isinstance(v, dict) else {}) for k, v in profiles.items()
            }
        else:
            self._profiles = {}
        last = obj.get("last_used")
        self._last_used = str(last) if isinstance(last, (str,)) else None

    def save(self) -> None:
        obj = {
            "profiles": {
                name: _jsonable(params) for name, params in self._profiles.items()
            },
            "last_used": self._last_used,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    # CRUD

    def list_profiles(self) -> List[str]:
        return sorted(self._profiles.keys())

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return dict(self._profiles.get(name, {})) if name in self._profiles else None

    def set(
        self, name: str, params: Mapping[str, Any], make_default_last: bool = False
    ) -> None:
        self._profiles[str(name)] = dict(params)
        if make_default_last:
            self._last_used = str(name)
        self.save()

    def delete(self, name: str) -> bool:
        if name in self._profiles:
            del self._profiles[name]
            if self._last_used == name:
                self._last_used = None
            self.save()
            return True
        return False

    def rename(self, old: str, new: str) -> bool:
        if old not in self._profiles:
            return False
        if new in self._profiles:
            # перезаписать?
            self._profiles[new] = self._profiles.pop(old)
        else:
            self._profiles[new] = self._profiles.pop(old)
        if self._last_used == old:
            self._last_used = new
        self.save()
        return True

    # last_used

    def get_last_used(self) -> Optional[str]:
        return self._last_used

    def set_last_used(self, name: Optional[str]) -> None:
        if name is not None and name not in self._profiles:
            raise KeyError(f"Профиль '{name}' не найден")
        self._last_used = name
        self.save()

    # Высокоуровневое API

    def get_effective_params(
        self,
        meta: ScriptMeta,
        profile: Optional[str] = None,
        overrides: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Вычисляет итоговые параметры:
        defaults (из meta) -> профиль (если задан) -> overrides (если заданы),
        затем приводит все значения к типам согласно метаданным.
        Неизвестные ключи игнорируются.
        """
        base = apply_defaults(meta)
        prof: Dict[str, Any] = {}
        if profile:
            prof = self.get(profile) or {}
        merged: Dict[str, Any] = {**base, **prof, **(dict(overrides or {}))}
        # Оставляем только ключи, описанные в метаданных, и приводим типы
        final = coerce_all_params(meta, merged)
        return final


__all__ = [
    "coerce_param_value",
    "apply_defaults",
    "coerce_all_params",
    "ParamProfiles",
]
