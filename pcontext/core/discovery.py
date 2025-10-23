from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .errors import MetadataError
from .metadata import parse_script_file
from .types import ScriptMeta


# -----------------------------
# Утилиты обхода каталогов
# -----------------------------

_IGNORED_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "env",
    "venv",
    ".env",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
}


def _should_skip_dir(name: str) -> bool:
    if not name:
        return True
    if name in _IGNORED_DIRS:
        return True
    if name.startswith("."):
        # скрытые системные каталоги тоже пропускаем
        return True
    return False


def _iter_py_files(root: Path) -> Iterable[Path]:
    """
    Итерирует .py файлы рекурсивно, пропуская игнорируемые каталоги.
    """
    stack: List[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if _should_skip_dir(name):
                                continue
                            stack.append(Path(entry.path))
                        else:
                            # Файлы
                            if entry.name.lower().endswith(".py"):
                                yield Path(entry.path)
                    except PermissionError:
                        continue
        except FileNotFoundError:
            # каталог исчез в процессе обхода
            continue
        except NotADirectoryError:
            continue


# -----------------------------
# Группы из пути
# -----------------------------


def derive_group_from_path(
    base_dirs: Sequence[Path], script_path: Path
) -> Optional[str]:
    """
    Вычисляет группу скрипта по положению в дереве относительно одного из базовых каталогов.
    Например:
      base = ~/PContext/scripts
      file = ~/PContext/scripts/Vision/YOLO/detect.py
      -> "Vision/YOLO"
    Если файл не попадает ни в один из base_dirs — возвращает None.
    """
    script_path = script_path.resolve()
    # Выбираем базовый каталог с максимальной длиной общего префикса
    best_base: Optional[Path] = None
    best_len = -1
    for base in base_dirs:
        try:
            base_res = Path(base).resolve()
        except Exception:
            continue
        try:
            rel = script_path.relative_to(base_res)
        except Exception:
            continue
        # Подходит — считаем глубину
        depth = len(rel.parts)
        if depth > best_len:
            best_len = depth
            best_base = base_res
    if best_base is None:
        return None

    rel = script_path.relative_to(best_base)
    # Папка скрипта (без имени файла)
    parent = rel.parent
    if parent == Path("."):
        return None
    # Формируем путь группы с разделителем "/"
    return "/".join(parent.parts)


# -----------------------------
# Обнаружение скриптов
# -----------------------------


def discover_scripts(script_dirs: Sequence[Path]) -> Tuple[List[ScriptMeta], List[str]]:
    """
    Обходит заданные каталоги и собирает метаданные скриптов.
    Возвращает кортеж: (список ScriptMeta, список сообщений об ошибках).
    utils.py и прочие .py без метаданных игнорируются (parse_script_file -> None).
    """
    metas: List[ScriptMeta] = []
    errors: List[str] = []

    # Нормализуем базовые каталоги (отфильтруем несуществующие)
    bases = [Path(p).expanduser() for p in script_dirs]
    bases = [p for p in bases if p.exists() and p.is_dir()]

    for base in bases:
        for file in _iter_py_files(base):
            try:
                meta = parse_script_file(file)
            except MetadataError as me:
                errors.append(str(me))
                continue
            except Exception as e:
                errors.append(f"{file}: {e}")
                continue

            if meta is None:
                # Вспомогательный файл
                continue

            # Если группа не задана явно — вычислим по пути
            if not meta.group:
                try:
                    grp = derive_group_from_path(bases, file)
                except Exception:
                    grp = None
                if grp:
                    meta.group = grp

            metas.append(meta)

    return metas, errors


# -----------------------------
# Watcher (опционально watchdog)
# -----------------------------


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"


@dataclass
class FileChangeEvent:
    change: ChangeType
    path: Path
    src_path: Optional[Path] = None
    dest_path: Optional[Path] = None


try:
    from watchdog.events import FileSystemEventHandler  # type: ignore
    from watchdog.observers import Observer  # type: ignore

    _WATCHDOG_AVAILABLE = True
except Exception:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore
    _WATCHDOG_AVAILABLE = False


class _Handler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, root: Path, callback: Callable[[FileChangeEvent], None]) -> None:
        super().__init__()
        self.root = Path(root)
        self.cb = callback

    # Учитываем только .py файлы
    def _accept(self, path: str) -> bool:
        try:
            p = Path(path)
            return p.suffix.lower() == ".py"
        except Exception:
            return False

    def on_created(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return
        if not self._accept(event.src_path):
            return
        self.cb(FileChangeEvent(change=ChangeType.ADDED, path=Path(event.src_path)))

    def on_modified(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return
        if not self._accept(event.src_path):
            return
        self.cb(FileChangeEvent(change=ChangeType.MODIFIED, path=Path(event.src_path)))

    def on_deleted(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return
        # Здесь event.src_path — удаленный путь
        if not self._accept(event.src_path):
            return
        self.cb(FileChangeEvent(change=ChangeType.REMOVED, path=Path(event.src_path)))

    def on_moved(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return
        # И переименование/перемещение
        if self._accept(event.src_path) or self._accept(event.dest_path):
            self.cb(
                FileChangeEvent(
                    change=ChangeType.RENAMED,
                    path=Path(event.dest_path),  # финальный путь
                    src_path=Path(event.src_path),
                    dest_path=Path(event.dest_path),
                )
            )


class ScriptDirWatcher:
    """
    Наблюдатель за каталогами скриптов (использует watchdog, если установлен).
    Вызывает callback(FileChangeEvent) при изменениях .py файлов.
    """

    def __init__(
        self,
        script_dirs: Sequence[Path],
        callback: Callable[[FileChangeEvent], None],
        recursive: bool = True,
    ) -> None:
        if not _WATCHDOG_AVAILABLE:
            raise RuntimeError(
                "watchdog не установлен. Установите 'watchdog' для слежения за каталогами скриптов."
            )
        self._script_dirs = [Path(p).expanduser() for p in script_dirs]
        self._script_dirs = [p for p in self._script_dirs if p.exists() and p.is_dir()]
        self._callback = callback
        self._recursive = recursive
        self._observer: Optional[Observer] = None  # type: ignore[assignment]
        self._handlers: List[_Handler] = []

    def start(self) -> None:
        if self._observer is not None:
            return
        self._observer = Observer()  # type: ignore[call-arg]
        for root in self._script_dirs:
            handler = _Handler(root, self._callback)
            self._handlers.append(handler)
            self._observer.schedule(handler, str(root), recursive=self._recursive)  # type: ignore[arg-type]
        self._observer.start()

    def stop(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=3.0)
        finally:
            self._observer = None
            self._handlers.clear()


__all__ = [
    "discover_scripts",
    "derive_group_from_path",
    "ChangeType",
    "FileChangeEvent",
    "ScriptDirWatcher",
]
