from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import data_dir
from .discovery import discover_scripts
from .types import AcceptsSpec, ParameterSpec, ScriptMeta, ScriptType
from .metadata import _build_accepts_spec, _build_params_spec  # type: ignore


REGISTRY_FILE = "registry.json"
REGISTRY_VERSION = 1


def _registry_path() -> Path:
    return data_dir() / REGISTRY_FILE


def _sanitize_id(s: str) -> str:
    import re

    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s or "script"


def _unique_id(suggested: str, path: Path, used: set[str]) -> str:
    sid = _sanitize_id(suggested)
    if sid not in used:
        used.add(sid)
        return sid
    import hashlib

    h = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    for attempt in range(1, 1000):
        cand = f"{sid}-{h[:min(8, 2 + attempt // 10)]}"
        if cand not in used:
            used.add(cand)
            return cand
    idx = 2
    while True:
        cand = f"{sid}-{idx}"
        if cand not in used:
            used.add(cand)
            return cand
        idx += 1


@dataclass
class ScriptEntry:
    id: str
    meta: ScriptMeta
    virtual: bool = False  # синтетический one-shot по actions сервиса
    source_service_id: Optional[str] = None
    action_id: Optional[str] = None

    @property
    def path(self) -> Path:
        assert self.meta.file_path is not None or self.virtual
        return (
            Path(self.meta.file_path)
            if self.meta.file_path
            else Path(f"<virtual:{self.id}>")
        )


class ScriptRegistry:
    """
    Реестр скриптов:
      - хранит ScriptMeta по id,
      - пересканирует каталоги (discovery),
      - добавляет виртуальные one-shot из actions сервисов,
      - сохраняет «тонкий» кэш на диск.
    """

    def __init__(self, script_dirs: Optional[Sequence[Path]] = None) -> None:
        self.script_dirs: List[Path] = [
            Path(p).expanduser() for p in (script_dirs or [])
        ]
        self._entries_by_id: Dict[str, ScriptEntry] = {}
        self._id_by_path: Dict[Path, str] = {}
        self.last_scan_ts: Optional[float] = None
        self.scan_errors: List[str] = []

    def rescan(
        self, script_dirs: Optional[Sequence[Path]] = None
    ) -> Tuple[List[ScriptEntry], List[str]]:
        if script_dirs is not None:
            self.script_dirs = [Path(p).expanduser() for p in script_dirs]

        metas, errors = discover_scripts(self.script_dirs)

        used_ids: set[str] = set()
        new_entries: Dict[str, ScriptEntry] = {}
        new_id_by_path: Dict[Path, str] = {}

        # 1) Реальные скрипты
        for m in metas:
            suggested = m.id or m.stable_id
            chosen_id = _unique_id(suggested, m.file_path or Path("."), used_ids)
            m.id = chosen_id
            e = ScriptEntry(id=chosen_id, meta=m, virtual=False)
            new_entries[chosen_id] = e
            if m.file_path:
                new_id_by_path[Path(m.file_path)] = chosen_id

        # 2) Виртуальные one-shot из actions сервисов
        for e in list(new_entries.values()):
            if e.meta.type is not ScriptType.SERVICE:
                continue
            svc = e.meta
            if not getattr(svc, "actions", None):
                continue
            for act in svc.actions:
                # Синтез ScriptMeta для пункта меню
                vm = ScriptMeta(
                    name=act.name,
                    type=ScriptType.ONE_SHOT,
                    accepts=act.accepts,
                    id=None,
                    description=act.description
                    or f"Action '{act.id}' of service '{svc.name}'",
                    group=act.group if act.group is not None else svc.group,
                    icon=act.icon if act.icon is not None else svc.icon,
                    proxy_service=svc.stable_id,
                    depends=e.meta.depends,  # можно добавить scripts=[svc.stable_id], но proxy_service уже достаточно
                    params=act.params,
                    timeout=svc.timeout,  # наследуем таймауты по умолчанию (one_shot_seconds не задан)
                    resources=svc.resources,
                    auto_open_result=svc.auto_open_result,
                    python_interpreter=svc.python_interpreter,
                    file_path=None,
                )
                # Служебно — запомним, какую функцию вызывать
                vm.proxy_entry = act.entry or None
                # Присвоим id
                suggested_id = f"{svc.stable_id}__act__{_sanitize_id(act.id)}"
                chosen_id = _unique_id(
                    suggested_id, Path(f"<virtual:{suggested_id}>"), used_ids
                )
                vm.id = chosen_id

                ve = ScriptEntry(
                    id=chosen_id,
                    meta=vm,
                    virtual=True,
                    source_service_id=svc.stable_id,
                    action_id=str(act.id),
                )
                new_entries[chosen_id] = ve
                # file_path отсутствует — в _id_by_path не регистрируем

        self._entries_by_id = new_entries
        self._id_by_path = new_id_by_path
        self.scan_errors = list(errors)
        self.last_scan_ts = time.time()

        self._save_thin_cache()
        return list(self._entries_by_id.values()), list(self.scan_errors)

    def list_entries(self) -> List[ScriptEntry]:
        return sorted(
            self._entries_by_id.values(),
            key=lambda e: ((e.meta.group or "").lower(), e.meta.name.lower()),
        )

    def get(self, script_id: str) -> Optional[ScriptEntry]:
        return self._entries_by_id.get(script_id)

    def find_by_path(self, path: Path) -> Optional[ScriptEntry]:
        sid = self._id_by_path.get(Path(path))
        return self._entries_by_id.get(sid) if sid else None

    def ids(self) -> List[str]:
        return sorted(self._entries_by_id.keys())

    def count(self) -> int:
        return len(self._entries_by_id)

    def groups_flat(self) -> Dict[str, List[ScriptEntry]]:
        groups: Dict[str, List[ScriptEntry]] = {}
        for e in self.list_entries():
            g = e.meta.group or ""
            groups.setdefault(g, []).append(e)
        return groups

    def groups_tree(self) -> Dict[str, dict]:
        root: Dict[str, dict] = {}
        for g, items in self.groups_flat().items():
            node = root
            if g:
                parts = g.split("/")
                for part in parts:
                    node = node.setdefault(part, {})
            else:
                node = root.setdefault("", {})
            node.setdefault("_items", [])
            node["_items"].extend(items)

        def ensure_items(d: dict) -> None:
            d.setdefault("_items", [])
            for k, v in list(d.items()):
                if k == "_items":
                    continue
                ensure_items(v)

        ensure_items(root)
        return root

    def filter_by_inputs(self, inputs) -> List[ScriptEntry]:
        metas = [e.meta for e in self._entries_by_id.values()]
        from .utils import filter_scripts_by_inputs  # локальный импорт

        matched = filter_scripts_by_inputs(metas, inputs)
        out: List[ScriptEntry] = []
        for m in matched:
            sid = None
            for e in self._entries_by_id.values():
                if e.meta.file_path == m.file_path and not e.virtual:
                    sid = e.id
                    break
                if e.virtual and e.meta.id == m.id:
                    sid = e.id
                    break
            if sid and sid in self._entries_by_id:
                out.append(self._entries_by_id[sid])
        return sorted(
            out, key=lambda e: ((e.meta.group or "").lower(), e.meta.name.lower())
        )

    def _save_thin_cache(self) -> None:
        path = _registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": REGISTRY_VERSION,
            "saved_ts": int(time.time()),
            "script_dirs": [str(p) for p in self.script_dirs],
            "scripts": [
                {
                    "id": e.id,
                    "path": str(e.path),
                    "name": e.meta.name,
                    "group": e.meta.group or "",
                    "type": e.meta.type.value,
                    "version_sig": e.meta.version_sig or "",
                    "virtual": bool(e.virtual),
                    "source_service_id": e.source_service_id,
                    "action_id": e.action_id,
                }
                for e in self.list_entries()
            ],
            "errors": list(self.scan_errors),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load_thin_cache(self) -> Optional[dict]:
        path = _registry_path()
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                return None
            return obj
        except Exception:
            return None

    def remove_by_id(self, script_id: str) -> bool:
        e = self._entries_by_id.pop(script_id, None)
        if not e:
            return False
        try:
            if e.meta.file_path and Path(e.meta.file_path) in self._id_by_path:
                del self._id_by_path[Path(e.meta.file_path)]
        except Exception:
            pass
        self._save_thin_cache()
        return True

    def remove_by_path(self, path: Path) -> bool:
        sid = self._id_by_path.get(Path(path))
        if not sid:
            return False
        return self.remove_by_id(sid)

    def upsert_from_meta(self, meta: ScriptMeta) -> ScriptEntry:
        sid = None
        if meta.file_path and Path(meta.file_path) in self._id_by_path:
            sid = self._id_by_path[Path(meta.file_path)]
        else:
            used = set(self._entries_by_id.keys())
            sid = _unique_id(
                meta.id or meta.stable_id, meta.file_path or Path("."), used
            )

        meta.id = sid
        e = ScriptEntry(id=sid, meta=meta, virtual=False)
        self._entries_by_id[sid] = e
        if meta.file_path:
            self._id_by_path[Path(meta.file_path)] = sid

        self._save_thin_cache()
        return e


__all__ = [
    "ScriptRegistry",
    "ScriptEntry",
]
