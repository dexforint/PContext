from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..core.config import data_dir


LOCK_FILE = "pcontextd.pid"


def _lock_path() -> Path:
    return data_dir() / LOCK_FILE


def _read_pid(path: Path) -> Optional[int]:
    try:
        txt = path.read_text(encoding="utf-8").strip()
        if not txt:
            return None
        return int(txt)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        pass

    if os.name != "nt":
        try:
            os.kill(pid, 0)  # type: ignore[arg-type]
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    # На Windows без psutil надежной проверки нет — считаем, что процесса нет
    return False


def _spawn_daemon() -> bool:
    """
    Запускает фоновый процесс демона/трея.
    """
    cmd = [sys.executable, "-m", "pcontext.daemon.main"]

    creationflags = 0
    kwargs = {}
    if os.name == "nt":
        # Запуск без консольного окна, в отдельной группе процессов
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
        kwargs["creationflags"] = creationflags
        kwargs["close_fds"] = True
    else:
        # POSIX: новый сессионный лидер (отвязка от родителя)
        kwargs["start_new_session"] = True
        kwargs["close_fds"] = True

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
        return True
    except Exception as e:
        sys.stderr.write(f"Не удалось запустить демон: {e}\n")
        return False


def cmd_start() -> int:
    lp = _lock_path()
    if lp.exists():
        pid = _read_pid(lp)
        if pid and _pid_alive(pid):
            print(f"Демон уже запущен (PID {pid}).")
            return 0
        # «Протухший» лок-файл — удалим
        try:
            lp.unlink(missing_ok=True)
        except Exception:
            pass

    ok = _spawn_daemon()
    if not ok:
        return 2

    # Небольшая задержка и проверка появления pid-файла
    for _ in range(20):
        time.sleep(0.1)
        if lp.exists():
            pid = _read_pid(lp)
            if pid and _pid_alive(pid):
                print(f"Демон запущен (PID {pid}).")
                return 0
    print("Демон запущен (PID неизвестен).")
    return 0


def _terminate_posix(pid: int, timeout: float = 3.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)  # type: ignore[arg-type]
    except ProcessLookupError:
        return True
    except Exception:
        pass
    # Ждем завершения
    start = time.time()
    while time.time() - start < timeout:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    # Форс-килл
    try:
        os.kill(pid, signal.SIGKILL)  # type: ignore[arg-type]
    except Exception:
        pass
    return not _pid_alive(pid)


def _terminate_windows(pid: int, timeout: float = 3.0) -> bool:
    # Предпочитаем psutil для аккуратного завершения
    try:
        import psutil  # type: ignore

        p = psutil.Process(pid)
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=timeout)
            return True
        except Exception:
            try:
                p.kill()
                p.wait(timeout=1.0)
                return True
            except Exception:
                return False
    except Exception:
        # Фолбэк — нет psutil, попытаемся через taskkill
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        # Проверим, жив ли процесс
        return not _pid_alive(pid)


def cmd_stop() -> int:
    lp = _lock_path()
    pid = _read_pid(lp) if lp.exists() else None
    if not pid:
        print("Демон не запущен.")
        return 0

    if os.name == "nt":
        ok = _terminate_windows(pid)
    else:
        ok = _terminate_posix(pid)

    # Удалим лок-файл
    try:
        lp.unlink(missing_ok=True)
    except Exception:
        pass

    print("Демон остановлен." if ok else "Не удалось остановить демон.")
    return 0 if ok else 1


def cmd_status() -> int:
    lp = _lock_path()
    pid = _read_pid(lp) if lp.exists() else None
    if pid and _pid_alive(pid):
        print(f"Статус: запущен (PID {pid}).")
        return 0
    print("Статус: не запущен.")
    return 1


def cmd_restart() -> int:
    cmd_stop()
    time.sleep(0.2)
    return cmd_start()


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pcontextd",
        description="Управление демоном/треем PContext",
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("start", help="Запустить демон (трей)")
    sub.add_parser("stop", help="Остановить демон")
    sub.add_parser("status", help="Показать статус демона")
    sub.add_parser("restart", help="Перезапустить демон")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    ns = _parse_args(argv)
    if ns.cmd == "start":
        return cmd_start()
    if ns.cmd == "stop":
        return cmd_stop()
    if ns.cmd == "status":
        return cmd_status()
    if ns.cmd == "restart":
        return cmd_restart()
    # Если команда не указана — покажем помощь
    _ = _parse_args(["-h"])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
