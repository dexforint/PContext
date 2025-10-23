from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union, List


# -----------------------------
# Базовые перечисления и алиасы
# -----------------------------


class ScriptType(enum.Enum):
    ONE_SHOT = "one-shot"
    SERVICE = "service"


class InputKind(enum.Enum):
    FILE = "file"
    DIRECTORY = "directory"
    BACKGROUND = "background"


class Scope(enum.Enum):
    FILES = "files"
    DIRECTORIES = "directories"
    BACKGROUND = "background"
    MIXED = "mixed"


class RunStatus(enum.Enum):
    OK = "OK"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class BatchMode(enum.Enum):
    BATCH = "batch"
    PER_ITEM = "per-item"


class ParamType(enum.Enum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    ENUM = "enum"
    SLIDER = "slider"
    TEXT = "text"
    FILE = "file"
    FOLDER = "folder"
    LIST_STR = "list[str]"
    LIST_INT = "list[int]"
    DICT = "dict"
    SECRET = "secret"


# Ключи допустимых «типизированных» результатов:
ALLOWED_RESULT_KEYS: Tuple[str, ...] = (
    "image",
    "video",
    "audio",
    "textfile",
    "pdf",
    "doc",
    "ppt",
    "xls",
    "archive",
    "folder",
    "link",
    "any",
)

ScriptId = str
Params = Dict[str, Any]


# -----------------------------
# Поддержка правил количества
# -----------------------------


@dataclass(frozen=True)
class CountSpec:
    expr: str = "*"

    def matches(self, n: int) -> bool:
        e = self.expr.strip()
        if e == "*":
            return True
        import re as _re

        m = _re.match(r"^\s*>?=\s*(\d+)\s*$", e)
        if m:
            return n >= int(m.group(1))
        m = _re.match(r"^\s*(\d+)\s*$", e)
        if m:
            return n == int(m.group(1))
        m = _re.match(r"^\s*(\d+)\s*\.\.\s*(\d+|\*)\s*$", e)
        if m:
            lo = int(m.group(1))
            hi_raw = m.group(2)
            if hi_raw == "*":
                return n >= lo
            return lo <= n <= int(hi_raw)
        return False


# -----------------------------
# Модели входов и метаданных
# -----------------------------


@dataclass
class Input:
    kind: InputKind
    path: Optional[Path] = None
    name: Optional[str] = None
    mime: Optional[str] = None
    size: Optional[int] = None
    created_ts: Optional[float] = None
    modified_ts: Optional[float] = None

    def __post_init__(self) -> None:
        if isinstance(self.path, str):
            self.path = Path(self.path)


@dataclass
class AcceptsSpec:
    scope: Scope = Scope.MIXED
    mimes: List[str] = field(default_factory=list)
    extensions: List[str] = field(default_factory=list)
    count: CountSpec = field(default_factory=lambda: CountSpec("*"))
    mode: BatchMode = BatchMode.BATCH

    def normalize(self) -> None:
        self.extensions = [e if e.startswith(".") else f".{e}" for e in self.extensions]
        self.extensions = [e.lower() for e in self.extensions]


@dataclass
class DependsSpec:
    pip: List[str] = field(default_factory=list)
    scripts: List[ScriptId] = field(default_factory=list)


@dataclass
class ParameterSpec:
    type: ParamType
    default: Any = None
    title: Optional[str] = None
    description: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: Optional[List[str]] = None
    regex: Optional[str] = None
    multiline: Optional[bool] = None
    placeholder: Optional[str] = None
    file_filter: Optional[str] = None
    hidden: bool = False
    secret: bool = False


@dataclass
class TimeoutSpec:
    one_shot_seconds: Optional[float] = None
    service_idle_seconds: Optional[float] = None
    grace_seconds: float = 5.0


@dataclass
class ResourceLocks:
    locks: List[str] = field(default_factory=list)


# --- Новое: «экспортируемые действия» сервиса (виртуальные пункты меню) ---


@dataclass
class ExposedAction:
    """
    Описание действия, которое сервис «экспортирует» как отдельный пункт меню.
    На основе таких действий реестр синтезирует виртуальные one-shot «прокси-скрипты»,
    которые прозрачно направляют вызов в сервис.

    Поля:
      id: стабильный id действия (используется при генерации id виртуального скрипта).
      name: отображаемое имя пункта меню.
      description: опционально.
      accepts: фильтр применимости (что должно быть выделено).
      params: параметры запроса (override относительно параметров сервиса).
      entry: имя функции в модуле сервиса, которую вызывать (по умолчанию pcontext_request).
      group: переопределение группы (если нужно отличать от группы сервиса).
      icon: иконка пункта (опционально).
    """

    id: str
    name: str
    accepts: AcceptsSpec
    params: Dict[str, ParameterSpec] = field(default_factory=dict)
    description: Optional[str] = None
    entry: Optional[str] = None
    group: Optional[str] = None
    icon: Optional[str] = None


@dataclass
class ScriptMeta:
    name: str
    type: ScriptType
    accepts: AcceptsSpec

    id: Optional[ScriptId] = None
    description: Optional[str] = None
    group: Optional[str] = None
    icon: Optional[str] = None

    # Мост (для one-shot): если задан, вызов направляется в указанный сервис
    proxy_service: Optional[str] = None
    proxy_entry: Optional[str] = (
        None  # если нужно вызывать не pcontext_request, а другую функцию сервиса
    )

    depends: DependsSpec = field(default_factory=DependsSpec)
    params: Dict[str, ParameterSpec] = field(default_factory=dict)
    timeout: TimeoutSpec = field(default_factory=TimeoutSpec)
    resources: ResourceLocks = field(default_factory=ResourceLocks)

    auto_open_result: Optional[bool] = True
    python_interpreter: Optional[str] = None

    # Экспортируемые действия (только для type=service)
    actions: List[ExposedAction] = field(default_factory=list)

    # Служебные:
    file_path: Optional[Path] = None
    version_sig: Optional[str] = None

    def normalize(self) -> None:
        self.accepts.normalize()
        if self.icon:
            self.icon = str(Path(self.icon))
        if self.file_path is not None:
            self.file_path = Path(self.file_path)

    @property
    def stable_id(self) -> str:
        if self.id:
            return self.id
        stem = (self.file_path.stem if self.file_path else self.name).lower()
        group = (self.group or "").strip().replace("/", "_").replace("\\", "_").lower()
        base = f"{group}__{stem}" if group else stem
        base = re.sub(r"[^a-z0-9_]+", "_", base).strip("_")
        return base or "script"


# -----------------------------
# Результаты выполнения
# -----------------------------

ResultMapping = Mapping[str, str]
ResultLike = Optional[Union[str, ResultMapping, Sequence[Union[str, ResultMapping]]]]


def is_url(text: str) -> bool:
    return bool(re.match(r"^(https?://|file://)", text, re.IGNORECASE))


def is_result_mapping(obj: Any) -> bool:
    if not isinstance(obj, Mapping):
        return False
    if not obj:
        return False
    for k, v in obj.items():
        if not isinstance(k, str) or k not in ALLOWED_RESULT_KEYS:
            return False
        if not isinstance(v, str):
            return False
    return True


def summarize_inputs(inputs: Sequence[Input]) -> str:
    if not inputs:
        return "no inputs (background)"
    kinds = {}
    for i in inputs:
        kinds[i.kind] = kinds.get(i.kind, 0) + 1
    parts = []
    if InputKind.FILE in kinds:
        parts.append(f"{kinds[InputKind.FILE]} file(s)")
    if InputKind.DIRECTORY in kinds:
        parts.append(f"{kinds[InputKind.DIRECTORY]} dir(s)")
    if InputKind.BACKGROUND in kinds:
        parts.append("background")
    names: List[str] = []
    for i in inputs[:3]:
        if i.path:
            names.append(Path(i.path).name)
    if names:
        parts.append("e.g. " + ", ".join(names))
    return " | ".join(parts)


__all__ = [
    "ScriptType",
    "InputKind",
    "Scope",
    "RunStatus",
    "BatchMode",
    "ParamType",
    "CountSpec",
    "Input",
    "AcceptsSpec",
    "DependsSpec",
    "ParameterSpec",
    "TimeoutSpec",
    "ResourceLocks",
    "ExposedAction",
    "ScriptMeta",
    "ALLOWED_RESULT_KEYS",
    "ResultMapping",
    "ResultLike",
    "is_url",
    "is_result_mapping",
    "summarize_inputs",
    "ScriptId",
    "Params",
]
