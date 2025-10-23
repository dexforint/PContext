from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from .errors import DependencyError


LineCallback = Optional[Callable[[str], None]]


@dataclass
class PipOptions:
    """
    Настройки запуска pip.
    """

    index_url: Optional[str] = None
    extra_index_urls: List[str] = field(default_factory=list)
    proxy: Optional[str] = None
    cache_dir: Optional[Path] = None
    # Дополнительные произвольные опции (например, "--trusted-host", и т.п.)
    extra_args: List[str] = field(default_factory=list)

    def to_args(self) -> List[str]:
        args: List[str] = [
            "--disable-pip-version-check",
        ]
        if self.index_url:
            args += ["--index-url", self.index_url]
        for url in self.extra_index_urls:
            args += ["--extra-index-url", url]
        if self.proxy:
            args += ["--proxy", self.proxy]
        if self.cache_dir:
            args += ["--cache-dir", str(self.cache_dir)]
        args += list(self.extra_args or [])
        return args


def _run_cmd(
    args: Sequence[str],
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    on_output: LineCallback = None,
) -> int:
    """
    Запускает внешний процесс и построчно проксирует stdout/stderr в on_output.
    Возвращает код возврата.
    """
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    # Гарантируем предсказуемую кодировку вывода
    merged_env.setdefault("PYTHONUTF8", "1")
    merged_env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    merged_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        # Не всплывающее окно консоли в Windows
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        creationflags=creationflags,
        startupinfo=startupinfo,  # type: ignore[arg-type]
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_output:
            # Линии уже в text режиме, убираем конечный \n
            on_output(line.rstrip("\r\n"))
    proc.stdout.close()
    return proc.wait()


def _venv_python_path(venv_path: Path) -> Path:
    """
    Возвращает путь к интерпретатору Python внутри виртуального окружения.
    """
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def pip_exec_args(python_exe: Path | str) -> List[str]:
    """
    Базовая команда запуска pip через интерпретатор venv: <python> -m pip
    """
    return [str(python_exe), "-m", "pip"]


def ensure_base_tools(
    venv_python: Path | str,
    opts: Optional[PipOptions] = None,
    on_output: LineCallback = None,
) -> None:
    """
    Обновляет базовые инструменты внутри окружения: pip, wheel, setuptools.
    """
    base_cmd = pip_exec_args(venv_python)
    args = base_cmd + ["install", "--upgrade", "pip", "setuptools", "wheel"]
    if opts:
        args += opts.to_args()
    rc = _run_cmd(args, on_output=on_output)
    if rc != 0:
        raise DependencyError(f"Не удалось обновить pip/setuptools/wheel (rc={rc})")


def pip_install(
    venv_python: Path | str,
    packages: Sequence[str],
    opts: Optional[PipOptions] = None,
    on_output: LineCallback = None,
    upgrade: bool = True,
) -> None:
    """
    Устанавливает список пакетов в указанное окружение.
    Бросает DependencyError при неуспехе.
    """
    pkgs = [p for p in packages if str(p).strip()]
    if not pkgs:
        return  # нечего ставить

    base_cmd = pip_exec_args(venv_python)
    cmd = base_cmd + ["install"]
    if upgrade:
        cmd.append("--upgrade")
    if opts:
        cmd += opts.to_args()
    cmd += list(pkgs)

    rc = _run_cmd(cmd, on_output=on_output)
    if rc != 0:
        raise DependencyError(f"pip install завершился с кодом {rc}")


def pip_freeze(
    venv_python: Path | str,
    on_output: LineCallback = None,
) -> List[str]:
    """
    Возвращает список установленных пакетов в формате pip freeze.
    """
    base_cmd = pip_exec_args(venv_python)
    cmd = base_cmd + ["freeze"]
    out_lines: List[str] = []

    def collect(line: str) -> None:
        out_lines.append(line)
        if on_output:
            on_output(line)

    rc = _run_cmd(cmd, on_output=collect)
    if rc != 0:
        # Возвращаем то, что собрали, даже если rc != 0
        # (freeze редко падает, но на всякий случай)
        pass
    # Очистим пустые строки
    return [ln for ln in out_lines if ln.strip()]


def pip_check(
    venv_python: Path | str,
    on_output: LineCallback = None,
) -> int:
    """
    Запускает 'pip check' и возвращает код возврата.
    0 — ок, иначе — проблемы с зависимостями.
    """
    base_cmd = pip_exec_args(venv_python)
    cmd = base_cmd + ["check"]
    return _run_cmd(cmd, on_output=on_output)


def pip_version(
    venv_python: Path | str,
    on_output: LineCallback = None,
) -> Optional[str]:
    """
    Возвращает версию pip в окружении.
    """
    base_cmd = pip_exec_args(venv_python)
    cmd = base_cmd + ["--version"]
    captured: List[str] = []

    def collect(line: str) -> None:
        captured.append(line)
        if on_output:
            on_output(line)

    rc = _run_cmd(cmd, on_output=collect)
    if rc != 0 or not captured:
        return None
    # Пример: "pip 24.0 from ... (python 3.11)"
    return captured[0].strip()


__all__ = [
    "PipOptions",
    "ensure_base_tools",
    "pip_install",
    "pip_freeze",
    "pip_check",
    "pip_version",
    "pip_exec_args",
]
