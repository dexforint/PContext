from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .config import EnvMode, wheels_cache_dir, venvs_root, load_config
from .deps import PipOptions, ensure_base_tools, pip_exec_args, pip_install, pip_version
from .errors import DependencyError, EnvironmentSetupError
from .types import ScriptMeta


LineCallback = Optional[Callable[[str], None]]

READY_MARKER = ".pcontext_ready"
INFO_FILE = "pcontext_env.json"


# -----------------------------
# Вспомогательные утилиты
# -----------------------------


def _sanitize_id(s: str) -> str:
    out = "".join(
        ch if ch.isalnum() or ch in ("-", "_", ".", "+") else "_" for ch in s.strip()
    )
    return out or "script"


def _venv_python_path(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def _normalize_packages(packages: Sequence[str]) -> List[str]:
    """
    Приводим список пакетов к каноническому виду для хэширования:
    - убираем пустые,
    - нормализуем пробелы,
    - сортируем регистронезависимо,
    - дедуплицируем, сохраняя порядок после сортировки.
    """
    seen = set()
    items = []
    for p in packages:
        s = str(p).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(s)
    items.sort(key=lambda x: x.lower())
    return items


def _python_version_id(python_exe: Path | str) -> str:
    """
    Возвращает идентификатор версии интерпретатора в виде 'pyXY', например 'py310', 'py311'.
    """
    try:
        cmd = [
            str(python_exe),
            "-c",
            "import sys; print(f'{sys.version_info[0]}{sys.version_info[1]}')",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        text = (res.stdout or "").strip()
        if text.isdigit():
            return f"py{text}"
    except Exception:
        pass
    # Фолбэк — используем текущий интерпретатор
    return f"py{sys.version_info[0]}{sys.version_info[1]}"


def compute_deps_hash(packages: Sequence[str], python_id: str) -> str:
    """
    Хэш зависимостей плюс версия интерпретатора. Короткий префикс SHA1.
    """
    pkgs = _normalize_packages(packages)
    data = (python_id + "\n" + "\n".join(pkgs)).encode("utf-8")
    h = hashlib.sha1(data).hexdigest()[:12]
    return f"{python_id}-{h}"


def _lock_path_for_env(env_path: Path) -> Path:
    # Используем «соседнюю» директорию .lock-<name> в корне venvs
    root = env_path.parent
    name = env_path.name
    return root / (".lock-" + name)


class _CreationLock:
    """
    Примитив «блокировки» на основе директории. Работает в пределах одной машины.
    """

    def __init__(
        self, lock_dir: Path, timeout: float = 600.0, poll_interval: float = 0.2
    ) -> None:
        self.lock_dir = lock_dir
        self.timeout = timeout
        self.poll = poll_interval
        self.acquired = False

    def acquire(self) -> bool:
        if self.acquired:
            return True
        start = time.time()
        while True:
            try:
                self.lock_dir.mkdir(parents=False, exist_ok=False)
                self.acquired = True
                # Отметим владельца (PID) — для диагностики
                try:
                    (self.lock_dir / "pid").write_text(
                        str(os.getpid()), encoding="utf-8"
                    )
                except Exception:
                    pass
                return True
            except FileExistsError:
                # Если лок уже есть — проверим таймаут
                if time.time() - start > self.timeout:
                    return False
                time.sleep(self.poll)
            except Exception:
                # Непредвиденная ошибка монтирования и т.п.
                if time.time() - start > self.timeout:
                    return False
                time.sleep(self.poll)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            # Удаляем файлы внутри и саму директорию
            for p in self.lock_dir.iterdir():
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
            self.lock_dir.rmdir()
        except Exception:
            # Игнорируем — не критично
            pass
        finally:
            self.acquired = False

    def __enter__(self) -> "_CreationLock":
        ok = self.acquire()
        if not ok:
            raise TimeoutError(f"Не удалось получить блокировку {self.lock_dir}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.release()


def _ensure_venv(
    python_exe: Path | str, env_path: Path, on_output: LineCallback = None
) -> None:
    """
    Создает виртуальное окружение, если оно отсутствует.
    """
    vpy = _venv_python_path(env_path)
    if vpy.exists():
        return
    # Создадим каталог окружения родительским Python
    cmd = [str(python_exe), "-m", "venv", str(env_path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        if on_output:
            if res.stdout:
                for ln in res.stdout.splitlines():
                    on_output(ln)
            if res.stderr:
                for ln in res.stderr.splitlines():
                    on_output(ln)
        raise EnvironmentSetupError(
            f"Не удалось создать venv по пути {env_path} (rc={res.returncode})"
        )


def _ensure_pip_present(
    venv_python: Path | str, on_output: LineCallback = None
) -> None:
    """
    Убеждаемся, что в окружении доступен pip. Если нет — используем ensurepip.
    """
    ver = pip_version(venv_python)
    if ver:
        return
    # Попробуем ensurepip
    cmd = [str(venv_python), "-m", "ensurepip", "--upgrade"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if on_output:
        if res.stdout:
            for ln in res.stdout.splitlines():
                on_output(ln)
        if res.stderr:
            for ln in res.stderr.splitlines():
                on_output(ln)
    if res.returncode != 0:
        raise EnvironmentSetupError(
            "pip недоступен и не удалось установить через ensurepip"
        )


def _write_env_info(env_path: Path, info: dict) -> None:
    try:
        (env_path / INFO_FILE).write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _read_env_info(env_path: Path) -> Optional[dict]:
    p = env_path / INFO_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


@dataclass
class EnvHandle:
    """
    Дескриптор окружения, возвращаемый ensure_environment().
    """

    path: Path
    python: Path
    kind: str  # "cached" или "per-script"
    key: str  # dep-hash (cached) или script-id (per-script)
    created: (
        bool  # True, если окружение только что создавалось/доводилось до готовности
    )


def _env_path_cached(dep_hash: str) -> Path:
    return venvs_root() / dep_hash


def _env_path_per_script(script_id: str) -> Path:
    return venvs_root() / "scripts" / _sanitize_id(script_id)


def _pip_options_from_config(cache_wheels: bool = True) -> PipOptions:
    """
    Формирует PipOptions из конфигурации пользователя:
      - pip_index_url, pip_extra_index_urls, pip_proxy,
      - общий кэш wheel'ов.
    """
    cfg = load_config(create_if_missing=True)
    cache_dir = wheels_cache_dir() if cache_wheels else None
    return PipOptions(
        index_url=cfg.pip_index_url or None,
        extra_index_urls=list(cfg.pip_extra_index_urls or []),
        proxy=cfg.pip_proxy or None,
        cache_dir=cache_dir,
        extra_args=[],
    )


def ensure_environment(
    meta: ScriptMeta,
    env_mode: EnvMode,
    on_output: LineCallback = None,
) -> EnvHandle:
    """
    Гарантирует наличие и готовность виртуального окружения для скрипта:
    - env_mode=CACHED: одно окружение на уникальный набор зависимостей + версию Python;
    - env_mode=PER_SCRIPT: отдельное окружение на скрипт.
    Учитываются зависимости meta.depends.pip.
    Возвращает EnvHandle(path, python, kind, key, created).
    """
    # Какой интерпретатор использовать для создания окружения
    creator_python = (
        Path(meta.python_interpreter)
        if meta.python_interpreter
        else Path(sys.executable)
    )
    py_id = _python_version_id(creator_python)

    pkgs = list(meta.depends.pip or [])
    pkgs_norm = _normalize_packages(pkgs)

    if env_mode == EnvMode.CACHED:
        key = compute_deps_hash(pkgs_norm, py_id)
        env_path = _env_path_cached(key)
        kind = "cached"
    else:
        key = meta.stable_id
        env_path = _env_path_per_script(key)
        kind = "per-script"

    vpy = _venv_python_path(env_path)
    ready_marker = env_path / READY_MARKER

    # Быстрый путь: готовое окружение
    if vpy.exists() and ready_marker.exists():
        return EnvHandle(path=env_path, python=vpy, kind=kind, key=key, created=False)

    # Настройки pip (index/proxy/extra-index + cache wheels)
    pip_opts = _pip_options_from_config()

    # Медленный путь: создаем/доводим окружение под блокировкой
    lock = _CreationLock(_lock_path_for_env(env_path))
    with lock:
        # Повторная проверка после получения замка
        vpy = _venv_python_path(env_path)
        if vpy.exists() and ready_marker.exists():
            return EnvHandle(
                path=env_path, python=vpy, kind=kind, key=key, created=False
            )

        # Создать venv, если нет
        _ensure_venv(creator_python, env_path, on_output=on_output)
        vpy = _venv_python_path(env_path)

        # Убедиться, что pip доступен, обновить базовые инструменты
        _ensure_pip_present(vpy, on_output=on_output)
        ensure_base_tools(vpy, opts=pip_opts, on_output=on_output)

        # Установить зависимости (если есть)
        if pkgs_norm:
            pip_install(
                vpy, pkgs_norm, opts=pip_opts, on_output=on_output, upgrade=True
            )

        # Записать информацию об окружении
        info = {
            "kind": kind,
            "key": key,
            "python_id": py_id,
            "created_with": str(creator_python),
            "deps": pkgs_norm,
            "created_ts": int(time.time()),
        }
        _write_env_info(env_path, info)

        # Пометить как готовое
        try:
            ready_marker.write_text("ok", encoding="utf-8")
        except Exception:
            pass

    return EnvHandle(path=env_path, python=vpy, kind=kind, key=key, created=True)


__all__ = [
    "EnvHandle",
    "compute_deps_hash",
    "ensure_environment",
]
