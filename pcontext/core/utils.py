from __future__ import annotations

import os
import stat
import uuid
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from .types import (
    AcceptsSpec,
    Input,
    InputKind,
    Scope,
    ScriptMeta,
)


# Попытка использовать python-magic для точного MIME (опционально)
try:
    import magic  # type: ignore

    _HAS_MAGIC = True
except Exception:
    magic = None  # type: ignore
    _HAS_MAGIC = False


# -----------------------------
# MIME и файловая информация
# -----------------------------


def guess_mime(path: Union[str, Path]) -> Optional[str]:
    """
    Определяет MIME-тип файла:
      - если доступен python-magic — используем его,
      - иначе — mimetypes по расширению.
    Для директорий возвращает "inode/directory".
    Если определить не удалось — None.
    """
    p = Path(path)
    try:
        if p.is_dir():
            return "inode/directory"
    except Exception:
        # Если нет прав или путь битый — продолжим ниже
        pass

    if _HAS_MAGIC:
        try:
            # prefer function that doesn't read file content when not necessary (mime=True reads header)
            return magic.from_file(str(p), mime=True)  # type: ignore[attr-defined]
        except Exception:
            pass

    # Fallback — по расширению
    mime, _enc = mimetypes.guess_type(str(p))
    return mime


def stat_path(
    path: Union[str, Path],
) -> Tuple[Optional[int], Optional[float], Optional[float]]:
    """
    Возвращает (size, created_ts, modified_ts) для файла/директории.
    В случае ошибок — (None, None, None).
    """
    p = Path(path)
    try:
        st = p.stat()
        size = None if stat.S_ISDIR(st.st_mode) else int(getattr(st, "st_size", 0))
        created = (
            float(getattr(st, "st_ctime", None))
            if getattr(st, "st_ctime", None)
            else None
        )
        modified = (
            float(getattr(st, "st_mtime", None))
            if getattr(st, "st_mtime", None)
            else None
        )
        return size, created, modified
    except Exception:
        return None, None, None


# -----------------------------
# Преобразование путей в Inputs
# -----------------------------


def build_input_for_path(path: Union[str, Path]) -> Input:
    """
    Создает Input из пути. Если путь не существует — трактуем как FILE с неизвестным MIME.
    """
    p = Path(path)
    try:
        if p.exists():
            if p.is_dir():
                size, cts, mts = stat_path(p)
                return Input(
                    kind=InputKind.DIRECTORY,
                    path=p,
                    name=p.name,
                    mime="inode/directory",
                    size=size,
                    created_ts=cts,
                    modified_ts=mts,
                )
            else:
                size, cts, mts = stat_path(p)
                return Input(
                    kind=InputKind.FILE,
                    path=p,
                    name=p.name,
                    mime=guess_mime(p),
                    size=size,
                    created_ts=cts,
                    modified_ts=mts,
                )
        else:
            # Не существует — пусть будет FILE с неизвестными метаданными
            return Input(kind=InputKind.FILE, path=p, name=p.name, mime=None)
    except Exception:
        # На случай неожиданных ошибок файловой системы
        return Input(kind=InputKind.FILE, path=p, name=p.name, mime=None)


def build_inputs(
    paths: Sequence[Union[str, Path]],
    scope: Scope,
) -> List[Input]:
    """
    Создает список Input на основе путей и требуемого scope.
    Для Scope.BACKGROUND возвращает [Input(BACKGROUND)] вне зависимости от paths.
    Для FILES/DIRECTORIES/MIXED — создает Inputs по каждому пути.
    """
    if scope is Scope.BACKGROUND:
        return [Input(kind=InputKind.BACKGROUND, path=None, name=None, mime=None)]
    inputs: List[Input] = []
    for p in paths:
        inputs.append(build_input_for_path(p))
    return inputs


def detect_invocation_scope(inputs: Sequence[Input]) -> Scope:
    """
    Определяет фактический scope по списку inputs.
    """
    if not inputs:
        return Scope.BACKGROUND
    kinds = {i.kind for i in inputs}
    if kinds == {InputKind.FILE}:
        return Scope.FILES
    if kinds == {InputKind.DIRECTORY}:
        return Scope.DIRECTORIES
    if kinds == {InputKind.BACKGROUND}:
        return Scope.BACKGROUND
    return Scope.MIXED


# -----------------------------
# Фильтрация по AcceptsSpec
# -----------------------------


def _ext_matches(path: Optional[Path], allowed_exts: Sequence[str]) -> bool:
    if not allowed_exts:
        return True
    if not path:
        return False
    ext = path.suffix.lower()
    return ext in allowed_exts


def _mime_matches(mime: Optional[str], allowed_mimes: Sequence[str]) -> bool:
    if not allowed_mimes:
        return True
    if not mime:
        return False
    m = mime.lower()
    for pat in allowed_mimes:
        pat = pat.lower().strip()
        if pat.endswith("/*"):
            # image/* — сравним по префиксу до '/'
            base = pat.split("/", 1)[0]
            if m.startswith(base + "/"):
                return True
        else:
            if m == pat:
                return True
    return False


def inputs_match_accepts(
    inputs: Sequence[Input],
    accepts: AcceptsSpec,
) -> bool:
    """
    Проверяет, подходят ли входные данные под декларативный фильтр accepts.
    """
    inv_scope = detect_invocation_scope(inputs)
    # Сопоставление scope
    if accepts.scope is Scope.BACKGROUND:
        if inv_scope is not Scope.BACKGROUND:
            return False
        # count: для background считаем N=0
        n = 0
        return accepts.count.matches(n)

    if accepts.scope is Scope.FILES:
        # Все inputs должны быть файлами
        if not inputs or any(i.kind is not InputKind.FILE for i in inputs):
            return False
    elif accepts.scope is Scope.DIRECTORIES:
        if not inputs or any(i.kind is not InputKind.DIRECTORY for i in inputs):
            return False
    elif accepts.scope is Scope.MIXED:
        # Разрешаем любую комбинацию, включая пустые (но count тогда проверит)
        pass

    # Количество (не считаем BACKGROUND элементы)
    n = sum(1 for i in inputs if i.kind is not InputKind.BACKGROUND)
    if not accepts.count.matches(n):
        return False

    # MIME/Extension — применяем только к файлам
    file_inputs = [i for i in inputs if i.kind is InputKind.FILE]
    if file_inputs:
        for i in file_inputs:
            ok_ext = _ext_matches(i.path, accepts.extensions)
            ok_mime = _mime_matches(i.mime, accepts.mimes)
            # Если одновременно заданы и extensions, и mimes — требуем соответствия обоим
            if accepts.extensions and accepts.mimes:
                if not (ok_ext and ok_mime):
                    return False
            else:
                # Иначе достаточно любого из фильтров (или отсутствующего)
                if not (ok_ext and ok_mime):
                    # Если один из фильтров пустой, он дает True
                    return False
    return True


def filter_scripts_by_inputs(
    metas: Sequence[ScriptMeta],
    inputs: Sequence[Input],
) -> List[ScriptMeta]:
    """
    Возвращает список скриптов, подходящих под входные данные по их AcceptsSpec.
    """
    out: List[ScriptMeta] = []
    for m in metas:
        try:
            if inputs_match_accepts(inputs, m.accepts):
                out.append(m)
        except Exception:
            # Если что-то пошло не так при проверке конкретного скрипта — пропустим его
            continue
    # Можно добавить сортировку: по группе/имени
    out.sort(key=lambda m: (m.group or "", m.name.lower()))
    return out


# -----------------------------
# Разное
# -----------------------------


def generate_run_id() -> str:
    """
    UUID без дефисов (короче и удобно в именах файлов).
    """
    return uuid.uuid4().hex


def now_iso() -> str:
    """
    Текущее время в ISO формате (для вспомогательных нужд).
    """
    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "guess_mime",
    "stat_path",
    "build_input_for_path",
    "build_inputs",
    "detect_invocation_scope",
    "inputs_match_accepts",
    "filter_scripts_by_inputs",
    "generate_run_id",
    "now_iso",
]
