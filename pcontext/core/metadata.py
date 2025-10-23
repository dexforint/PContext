from __future__ import annotations

import ast
import hashlib
import tokenize
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union, List

from .errors import MetadataError
from .types import (
    AcceptsSpec,
    BatchMode,
    CountSpec,
    DependsSpec,
    ExposedAction,
    ParameterSpec,
    ResourceLocks,
    ScriptMeta,
    Scope,
    ScriptType,
    TimeoutSpec,
)


try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    yaml = None


def _extract_yaml_frontmatter(docstring: Optional[str]) -> Optional[Dict[str, Any]]:
    if not docstring:
        return None
    text = docstring.strip()
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        raise MetadataError("Незакрытый YAML front matter в docstring")
    yaml_text = "\n".join(lines[1:end_idx]).strip()
    if not yaml_text:
        return {}
    if yaml is None:
        raise MetadataError(
            "Требуется PyYAML для чтения метаданных в docstring. "
            "Установите пакет 'PyYAML' или используйте PCTX = {...}."
        )
    try:
        data = yaml.safe_load(yaml_text)
    except Exception as e:
        raise MetadataError(f"Ошибка парсинга YAML front matter: {e}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise MetadataError("YAML front matter должен быть объектом (mapping).")
    return data


def _extract_pctx_from_ast(mod: ast.Module) -> Optional[Dict[str, Any]]:
    for node in mod.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PCTX":
                    try:
                        value = ast.literal_eval(node.value)
                    except Exception as e:
                        raise MetadataError(f"Не удалось разобрать PCTX: {e}")
                    if value is None:
                        return {}
                    if not isinstance(value, dict):
                        raise MetadataError("PCTX должен быть словарем (dict).")
                    return value
        if isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "PCTX"
                and node.value is not None
            ):
                try:
                    value = ast.literal_eval(node.value)
                except Exception as e:
                    raise MetadataError(f"Не удалось разобрать PCTX: {e}")
                if value is None:
                    return {}
                if not isinstance(value, dict):
                    raise MetadataError("ПCTX должен быть словарем (dict).")
                return value
    return None


def _norm_key(s: str) -> str:
    return s.strip().lower().replace("_", "-").replace(" ", "-")


def _parse_script_type(value: str) -> ScriptType:
    key = _norm_key(value)
    if key in ("one-shot", "oneshot", "one-shot-script"):
        return ScriptType.ONE_SHOT
    if key in ("service", "daemon", "server"):
        return ScriptType.SERVICE
    raise MetadataError(f"Неизвестный type скрипта: {value!r}")


def _parse_scope(value: str) -> Scope:
    key = _norm_key(value)
    if key in ("files",):
        return Scope.FILES
    if key in ("directories", "dirs", "folders"):
        return Scope.DIRECTORIES
    if key in ("background", "bg", "empty"):
        return Scope.BACKGROUND
    if key in ("mixed", "any"):
        return Scope.MIXED
    raise MetadataError(f"Неизвестный accepts.scope: {value!r}")


def _parse_batch_mode(value: str) -> BatchMode:
    key = _norm_key(value)
    if key in ("batch",):
        return BatchMode.BATCH
    if key in ("per-item", "peritem", "item-by-item"):
        return BatchMode.PER_ITEM
    raise MetadataError(f"Неизвестный accepts.mode: {value!r}")


_PARAM_TYPE_ALIASES = {
    "bool": "bool",
    "boolean": "bool",
    "int": "int",
    "integer": "int",
    "float": "float",
    "number": "float",
    "str": "str",
    "string": "str",
    "enum": "enum",
    "slider": "slider",
    "text": "text",
    "file": "file",
    "folder": "folder",
    "list[str]": "list[str]",
    "list[int]": "list[int]",
    "list-string": "list[str]",
    "list-int": "list[int]",
    "dict": "dict",
    "map": "dict",
    "secret": "secret",
}


from .types import ParamType  # after aliases


def _parse_param_type(value: str) -> ParamType:
    key = _norm_key(value)
    key = _PARAM_TYPE_ALIASES.get(key, key)
    try:
        return ParamType(key)
    except Exception:
        raise MetadataError(f"Неизвестный тип параметра: {value!r}")


def _build_accepts_spec(d: Mapping[str, Any] | None) -> AcceptsSpec:
    if not d:
        return AcceptsSpec()
    scope = d.get("scope", "mixed")
    mimes = list(d.get("mimes", []) or [])
    exts = list(d.get("extensions", []) or [])
    count_expr = d.get("count", "*")
    mode = d.get("mode", "batch")

    spec = AcceptsSpec(
        scope=_parse_scope(scope),
        mimes=[str(x) for x in mimes],
        extensions=[str(x) for x in exts],
        count=CountSpec(str(count_expr)),
        mode=_parse_batch_mode(str(mode)),
    )
    spec.normalize()
    return spec


def _build_depends_spec(d: Mapping[str, Any] | None) -> DependsSpec:
    if not d:
        return DependsSpec()
    pip_list = list(d.get("pip", []) or [])
    scripts_list = list(d.get("scripts", []) or [])
    return DependsSpec(
        pip=[str(x) for x in pip_list],
        scripts=[str(x) for x in scripts_list],
    )


def _build_parameter_spec(name: str, d: Mapping[str, Any]) -> ParameterSpec:
    if "type" not in d:
        raise MetadataError(f"Параметр {name!r} не содержит обязательного поля 'type'")
    ptype = _parse_param_type(str(d["type"]))

    spec = ParameterSpec(
        type=ptype,
        default=d.get("default", None),
        title=d.get("title"),
        description=d.get("description"),
        min=d.get("min"),
        max=d.get("max"),
        step=d.get("step"),
        options=list(d.get("options", []) or []) if "options" in d else None,
        regex=d.get("regex"),
        multiline=bool(d.get("multiline")) if "multiline" in d else None,
        placeholder=d.get("placeholder"),
        file_filter=d.get("file_filter"),
        hidden=bool(d.get("hidden", False)),
        secret=bool(d.get("secret", False)),
    )
    return spec


def _build_params_spec(d: Mapping[str, Any] | None) -> Dict[str, ParameterSpec]:
    if not d:
        return {}
    out: Dict[str, ParameterSpec] = {}
    for k, v in d.items():
        if not isinstance(v, Mapping):
            raise MetadataError(f"Ожидался mapping для параметра {k!r}")
        out[str(k)] = _build_parameter_spec(str(k), v)
    return out


def _build_timeout_spec(d: Mapping[str, Any] | None) -> TimeoutSpec:
    if not d:
        return TimeoutSpec()
    return TimeoutSpec(
        one_shot_seconds=d.get("one_shot_seconds"),
        service_idle_seconds=d.get("service_idle_seconds"),
        grace_seconds=float(d.get("grace_seconds", 5.0)),
    )


def _build_resources(d: Mapping[str, Any] | None) -> ResourceLocks:
    if not d:
        return ResourceLocks()
    locks = list(d.get("locks", []) or [])
    return ResourceLocks(locks=[str(x) for x in locks])


def _build_actions(d: Any) -> List[ExposedAction]:
    """
    Парсинг массива actions из метаданных сервиса.
    Формат элемента:
      - id: str (обяз.)
      - name: str (обяз.)
      - accepts: mapping (обяз.)
      - params: mapping (необяз.)
      - description: str (необяз.)
      - entry: str (необяз., по умолчанию pcontext_request)
      - group: str (необяз.)
      - icon: str (необяз.)
    """
    actions: List[ExposedAction] = []
    if not d:
        return actions
    if not isinstance(d, list):
        raise MetadataError("Поле 'actions' должно быть списком.")
    for idx, item in enumerate(d):
        if not isinstance(item, Mapping):
            raise MetadataError(f"Элемент 'actions[{idx}]' должен быть объектом.")
        aid = item.get("id")
        aname = item.get("name")
        aacc = item.get("accepts")
        if not aid or not aname or not isinstance(aacc, Mapping):
            raise MetadataError(
                f"Элемент actions[{idx}] должен содержать id, name и accepts."
            )
        accepts = _build_accepts_spec(aacc)
        params = _build_params_spec(
            item.get("params") if isinstance(item.get("params"), Mapping) else None
        )
        action = ExposedAction(
            id=str(aid),
            name=str(aname),
            accepts=accepts,
            params=params,
            description=(
                str(item.get("description"))
                if item.get("description") is not None
                else None
            ),
            entry=str(item.get("entry")) if item.get("entry") is not None else None,
            group=str(item.get("group")) if item.get("group") is not None else None,
            icon=str(item.get("icon")) if item.get("icon") is not None else None,
        )
        actions.append(action)
    return actions


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_script_file(file_path: Union[str, Path]) -> Optional[ScriptMeta]:
    path = Path(file_path)
    if path.suffix.lower() != ".py":
        return None
    try:
        with tokenize.open(str(path)) as f:
            source = f.read()
    except Exception as e:
        raise MetadataError(f"Не удалось прочитать файл {path}: {e}", file_path=path)
    try:
        mod = ast.parse(source, filename=str(path))
    except Exception as e:
        raise MetadataError(f"Не удалось распарсить Python AST: {e}", file_path=path)

    docstring = ast.get_docstring(mod)
    meta_dict: Optional[Dict[str, Any]] = None
    if docstring:
        meta_dict = _extract_yaml_frontmatter(docstring)
    if meta_dict is None:
        meta_dict = _extract_pctx_from_ast(mod)
    if meta_dict is None:
        return None

    try:
        meta = _meta_from_dict(meta_dict or {}, path)
    except MetadataError as me:
        me.file_path = path  # type: ignore[attr-defined]
        raise

    try:
        meta.version_sig = _file_sha256(path)[:12]
    except Exception:
        meta.version_sig = None

    meta.normalize()
    return meta


def _meta_from_dict(d: Mapping[str, Any], file_path: Path) -> ScriptMeta:
    if "type" not in d:
        raise MetadataError('Отсутствует обязательное поле "type"')
    if "name" not in d:
        raise MetadataError('Отсутствует обязательное поле "name"')

    script_type = _parse_script_type(str(d["type"]))
    name = str(d["name"])

    accepts = _build_accepts_spec(
        d.get("accepts") if isinstance(d.get("accepts"), Mapping) else None
    )
    depends = _build_depends_spec(
        d.get("depends") if isinstance(d.get("depends"), Mapping) else None
    )
    params = _build_params_spec(
        d.get("params") if isinstance(d.get("params"), Mapping) else None
    )
    timeout = _build_timeout_spec(
        d.get("timeout") if isinstance(d.get("timeout"), Mapping) else None
    )
    resources = _build_resources(
        d.get("resources") if isinstance(d.get("resources"), Mapping) else None
    )
    actions = _build_actions(d.get("actions")) if "actions" in d else []

    meta = ScriptMeta(
        name=name,
        type=script_type,
        accepts=accepts,
        id=str(d["id"]) if "id" in d and d["id"] is not None else None,
        description=(
            str(d["description"])
            if "description" in d and d["description"] is not None
            else None
        ),
        group=str(d["group"]) if "group" in d and d["group"] is not None else None,
        icon=str(d["icon"]) if "icon" in d and d["icon"] is not None else None,
        proxy_service=(
            str(d["proxy_service"])
            if "proxy_service" in d and d["proxy_service"] is not None
            else None
        ),
        depends=depends,
        params=params,
        timeout=timeout,
        resources=resources,
        auto_open_result=bool(d.get("auto_open_result", True)),
        python_interpreter=(
            str(d["python_interpreter"])
            if "python_interpreter" in d and d["python_interpreter"] is not None
            else None
        ),
        file_path=file_path,
        actions=actions,
    )
    # Поддержка необязательного поля proxy_entry, если кто-то его задаст в YAML
    if "proxy_entry" in d and d["proxy_entry"] is not None:
        meta.proxy_entry = str(d["proxy_entry"])
    return meta


__all__ = [
    "parse_script_file",
]
