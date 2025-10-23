from __future__ import annotations

import time
from typing import Optional

try:
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
except Exception:  # pragma: no cover
    QtCore = None  # type: ignore
    QtGui = None  # type: ignore
    QtWidgets = None  # type: ignore

# Импортируем по имени, чтобы избежать циклов при type checking
try:
    from ...core.services import ServiceManager  # type: ignore
except Exception:
    ServiceManager = object  # type: ignore


def _fmt_sec(s: float) -> str:
    s = max(0, int(s))
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{ss:02d}"
    return f"{m:02d}:{ss:02d}"


class ServicesView(QtWidgets.QWidget):  # type: ignore[misc]
    """
    Панель для отображения и управления активными сервисами.
    - Список: Имя, ID, Uptime, Idle, Статус.
    - Кнопки: Обновить, Остановить выбранный, Остановить все.
    - Автообновление каждые N секунд (по умолчанию 2с).
    """

    def __init__(
        self, services: ServiceManager, parent=None, refresh_interval_ms: int = 2000
    ) -> None:
        if QtWidgets is None:
            raise RuntimeError(
                "Для ServicesView требуется PySide6 (pip install PySide6)"
            )
        super().__init__(parent)
        self.services = services

        self.setWindowTitle("PContext — Сервисы")
        v = QtWidgets.QVBoxLayout(self)

        # Таблица
        self.table = QtWidgets.QTreeWidget(self)
        self.table.setHeaderLabels(["Имя", "ID", "Uptime", "Idle", "Статус"])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)  # type: ignore[attr-defined]
        self.table.setUniformRowHeights(True)
        self.table.setColumnCount(5)
        v.addWidget(self.table, 1)

        # Кнопки
        hb = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("Обновить", self)
        self.btn_stop = QtWidgets.QPushButton("Остановить выбранный", self)
        self.btn_stop_all = QtWidgets.QPushButton("Остановить все", self)
        hb.addWidget(self.btn_refresh)
        hb.addStretch(1)
        hb.addWidget(self.btn_stop)
        hb.addWidget(self.btn_stop_all)
        v.addLayout(hb)

        # Сигналы
        self.btn_refresh.clicked.connect(self.refresh)  # type: ignore[attr-defined]
        self.btn_stop.clicked.connect(self._stop_selected)  # type: ignore[attr-defined]
        self.btn_stop_all.clicked.connect(self._stop_all)  # type: ignore[attr-defined]
        self.table.itemActivated.connect(
            lambda _item, _col: self._stop_selected()
        )  # Enter/двойной клик  # type: ignore[attr-defined]

        # Таймер автообновления
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(int(refresh_interval_ms))
        self._timer.timeout.connect(self.refresh)  # type: ignore[attr-defined]
        self._timer.start()

        self.refresh()

    def _current_id(self) -> Optional[str]:
        it = self.table.currentItem()
        if it is None:
            return None
        return str(it.data(0, QtCore.Qt.UserRole))  # type: ignore[attr-defined]

    def refresh(self) -> None:
        self.table.clear()
        now = time.time()
        # Берем снапшот активных сервисов
        items = []
        for sid, handle in list(self.services._services.items()):
            if not handle.is_alive():
                continue
            name = handle.meta.name
            uptime = now - handle.start_ts
            idle = now - handle.last_used_ts
            status = "RUNNING" if handle.is_alive() else "STOPPED"
            row = QtWidgets.QTreeWidgetItem([name, sid, _fmt_sec(uptime), _fmt_sec(idle), status])  # type: ignore[call-arg]
            row.setData(0, QtCore.Qt.UserRole, sid)  # type: ignore[attr-defined]
            items.append(row)
        if not items:
            empty = QtWidgets.QTreeWidgetItem(["Нет активных сервисов", "", "", "", ""])  # type: ignore[call-arg]
            empty.setFlags(empty.flags() & ~QtCore.Qt.ItemIsSelectable)  # type: ignore[attr-defined]
            self.table.addTopLevelItem(empty)
        else:
            for it in items:
                self.table.addTopLevelItem(it)
            self.table.resizeColumnToContents(0)
            self.table.resizeColumnToContents(2)
            self.table.resizeColumnToContents(3)

    def _stop_selected(self) -> None:
        sid = self._current_id()
        if not sid:
            return
        ok = self.services.shutdown(sid, grace_timeout=3.0)
        msg = f"{sid}: {'остановлен' if ok else 'ошибка остановки'}"
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), msg)  # type: ignore[attr-defined]
        self.refresh()

    def _stop_all(self) -> None:
        self.services.shutdown_all(grace_timeout=3.0)
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Все сервисы остановлены")  # type: ignore[attr-defined]
        self.refresh()


__all__ = [
    "ServicesView",
]
