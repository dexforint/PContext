from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ...os_integration.common.shell_open import open_path

try:
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
except Exception:  # pragma: no cover
    QtCore = None  # type: ignore
    QtGui = None  # type: ignore
    QtWidgets = None  # type: ignore

# psutil — опционален. Без него окно покажет понятное сообщение.
try:
    import psutil  # type: ignore

    _HAS_PSUTIL = True
except Exception:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False


def _fmt_sec(s: float) -> str:
    s = max(0, int(s))
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{ss:02d}"
    return f"{m:02d}:{ss:02d}"


@dataclass
class TaskInfo:
    pid: int
    kind: str  # "one-shot" | "service" | "unknown"
    started: float
    script_path: Optional[Path]
    script_name: str
    payload_path: Optional[Path]
    cancel_flag_path: Optional[Path]
    log_file: Optional[Path]
    status: str  # "RUNNING" | "UNKNOWN"


def _parse_payload(
    payload_path: Path,
) -> Tuple[Optional[Path], Optional[Path], Optional[Path], Optional[str]]:
    """
    Возвращает (script_path, cancel_flag_path, log_file, script_name) из payload.json worker'а.
    """
    try:
        obj = json.loads(payload_path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return None, None, None, None
        script_p = obj.get("script_path")
        ctx = obj.get("ctx") or {}
        cflag = ctx.get("cancel_flag_path")
        logf = ctx.get("log_file")
        sname = None
        try:
            # имя скрипта — имя файла без расширения
            if script_p:
                sname = Path(script_p).name
        except Exception:
            sname = None
        return (
            Path(script_p) if script_p else None,
            Path(cflag) if cflag else None,
            Path(logf) if logf else None,
            sname,
        )
    except Exception:
        return None, None, None, None


def _discover_tasks() -> List[TaskInfo]:
    """
    Находит активные процессы воркеров pcontext:
      - python_worker.py (one-shot)
      - service_worker.py (service)
    Использует psutil для перебора процессов. Возвращает список TaskInfo.
    """
    tasks: List[TaskInfo] = []
    if not _HAS_PSUTIL:
        return tasks

    for p in psutil.process_iter(attrs=["pid", "create_time", "cmdline"]):  # type: ignore[attr-defined]
        try:
            cmd = p.info.get("cmdline") or []
            if not isinstance(cmd, list) or len(cmd) < 2:
                continue
            # Ищем путь к воркеру
            worker_kind = None
            payload_path: Optional[Path] = None

            # Вариант запуска: <python> <.../python_worker.py> --payload <path>
            # или <python> <.../service_worker.py> --payload <path>
            for i, tok in enumerate(cmd):
                low = str(tok).replace("\\", "/").lower()
                if low.endswith("/pcontext/workers/python_worker.py") or low.endswith(
                    "python_worker.py"
                ):
                    worker_kind = "one-shot"
                if low.endswith("/pcontext/workers/service_worker.py") or low.endswith(
                    "service_worker.py"
                ):
                    worker_kind = "service"
                if tok == "--payload" and i + 1 < len(cmd):
                    payload_path = Path(cmd[i + 1])
            if worker_kind is None or payload_path is None:
                continue

            script_path, cancel_flag, log_file, sname = _parse_payload(payload_path)
            info = TaskInfo(
                pid=int(p.info.get("pid")),
                kind=worker_kind,
                started=float(p.info.get("create_time") or time.time()),
                script_path=script_path,
                script_name=sname or (script_path.name if script_path else "unknown"),
                payload_path=payload_path,
                cancel_flag_path=cancel_flag,
                log_file=log_file,
                status="RUNNING",
            )
            tasks.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[attr-defined]
            continue
        except Exception:
            continue
    # Отсортируем по времени старта (новые сверху)
    tasks.sort(key=lambda t: t.started, reverse=True)
    return tasks


class TasksView(QtWidgets.QWidget):  # type: ignore[misc]
    """
    Окно просмотра текущих задач (one-shot) и сервисов-воркеров на уровне процессов.
    - Требует psutil для корректной работы.
    Возможности:
      - посмотреть список активных задач,
      - открыть лог запуска,
      - запросить мягкую отмену (создать cancel.flag),
      - принудительно завершить процесс.
    """

    def __init__(self, parent=None, refresh_interval_ms: int = 1500) -> None:
        if QtWidgets is None:
            raise RuntimeError("Для TasksView требуется PySide6 (pip install PySide6)")
        super().__init__(parent)
        self.setWindowTitle("PContext — Задачи")
        self.resize(780, 420)

        v = QtWidgets.QVBoxLayout(self)

        if not _HAS_PSUTIL:
            lbl = QtWidgets.QLabel(
                "Для отображения текущих задач установите зависимость: pip install psutil",
                self,
            )
            lbl.setStyleSheet("color: #c00;")
            v.addWidget(lbl)
            return

        # Таблица задач
        self.table = QtWidgets.QTreeWidget(self)
        self.table.setHeaderLabels(["Скрипт", "PID", "Тип", "Запущен", "Прошло", "Лог"])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)  # type: ignore[attr-defined]
        self.table.setUniformRowHeights(True)
        self.table.setColumnCount(6)
        v.addWidget(self.table, 1)

        # Кнопки действий
        hb = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("Обновить", self)
        self.btn_open_log = QtWidgets.QPushButton("Открыть лог", self)
        self.btn_cancel = QtWidgets.QPushButton("Отменить", self)
        self.btn_kill = QtWidgets.QPushButton("Принудительно завершить", self)
        hb.addWidget(self.btn_refresh)
        hb.addStretch(1)
        hb.addWidget(self.btn_open_log)
        hb.addWidget(self.btn_cancel)
        hb.addWidget(self.btn_kill)
        v.addLayout(hb)

        # Таймер автообновления
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(int(refresh_interval_ms))
        self._timer.timeout.connect(self.refresh)  # type: ignore[attr-defined]
        self._timer.start()

        # Сигналы
        self.btn_refresh.clicked.connect(self.refresh)  # type: ignore[attr-defined]
        self.btn_open_log.clicked.connect(self._open_log)  # type: ignore[attr-defined]
        self.btn_cancel.clicked.connect(self._cancel_task)  # type: ignore[attr-defined]
        self.btn_kill.clicked.connect(self._kill_task)  # type: ignore[attr-defined]
        self.table.itemActivated.connect(lambda _it, _col: self._open_log())  # type: ignore[attr-defined]

        self.refresh()

    def _current_task(self) -> Optional[TaskInfo]:
        it = self.table.currentItem()
        if it is None:
            return None
        payload = it.data(0, QtCore.Qt.UserRole)  # type: ignore[attr-defined]
        return payload if isinstance(payload, TaskInfo) else None

    def refresh(self) -> None:
        if not _HAS_PSUTIL:
            return
        self.table.clear()
        tasks = _discover_tasks()
        now = time.time()

        if not tasks:
            empty = QtWidgets.QTreeWidgetItem(["Нет активных задач", "", "", "", "", ""])  # type: ignore[call-arg]
            empty.setFlags(empty.flags() & ~QtCore.Qt.ItemIsSelectable)  # type: ignore[attr-defined]
            self.table.addTopLevelItem(empty)
            return

        for t in tasks:
            started_dt = time.strftime("%H:%M:%S", time.localtime(t.started))
            elapsed = _fmt_sec(now - t.started)
            log_short = str(t.log_file) if t.log_file else ""
            row = QtWidgets.QTreeWidgetItem([t.script_name, str(t.pid), t.kind, started_dt, elapsed, log_short])  # type: ignore[call-arg]
            row.setData(0, QtCore.Qt.UserRole, t)  # type: ignore[attr-defined]
            self.table.addTopLevelItem(row)

        # Поджать колонки
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self.table.resizeColumnToContents(2)
        self.table.resizeColumnToContents(4)

    def _open_log(self) -> None:
        t = self._current_task()
        if not t or not t.log_file:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Лог недоступен")  # type: ignore[attr-defined]
            return
        # Откроем сам файл, если ассоциации есть; иначе — папку
        ok = open_path(t.log_file)
        if not ok:
            open_path(t.log_file.parent)

    def _cancel_task(self) -> None:
        t = self._current_task()
        if not t:
            return
        if t.kind != "one-shot":
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Отмена поддерживается только для одноразовых задач")  # type: ignore[attr-defined]
            return
        if not t.cancel_flag_path:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Флаг отмены недоступен")  # type: ignore[attr-defined]
            return
        try:
            t.cancel_flag_path.parent.mkdir(parents=True, exist_ok=True)
            t.cancel_flag_path.write_text("cancel", encoding="utf-8")
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Отмена запрошена")  # type: ignore[attr-defined]
        except Exception as e:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), f"Ошибка отмены: {e}")  # type: ignore[attr-defined]

    def _kill_task(self) -> None:
        if not _HAS_PSUTIL:
            return
        t = self._current_task()
        if not t:
            return
        try:
            p = psutil.Process(t.pid)  # type: ignore[attr-defined]
            try:
                p.terminate()
                p.wait(timeout=2.0)
                QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Процесс завершен")  # type: ignore[attr-defined]
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=1.0)
                    QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Процесс убит")  # type: ignore[attr-defined]
                except Exception as e:
                    QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), f"Не удалось завершить: {e}")  # type: ignore[attr-defined]
        except Exception as e:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), f"Ошибка доступа к процессу: {e}")  # type: ignore[attr-defined]


__all__ = [
    "TasksView",
]
