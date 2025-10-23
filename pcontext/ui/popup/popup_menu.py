from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ...core.types import ScriptMeta

# PySide6 — опционально. Если недоступна, бросим понятную ошибку при вызове.
try:
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
except Exception:  # pragma: no cover
    QtCore = None  # type: ignore
    QtGui = None  # type: ignore
    QtWidgets = None  # type: ignore


@dataclass
class _Item:
    label: str
    meta: ScriptMeta


class _PopupDialog(QtWidgets.QDialog):  # type: ignore[misc]
    def __init__(self, items: List[_Item], parent=None) -> None:
        super().__init__(parent)

        self.setWindowTitle("PContext — выбрать скрипт")
        self.setWindowFlag(QtCore.Qt.Tool, True)  # type: ignore[attr-defined]
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)  # type: ignore[attr-defined]
        # Компактные отступы
        self.setContentsMargins(6, 6, 6, 6)

        self._items = items
        self._view_indices: List[int] = []
        self._chosen: Optional[ScriptMeta] = None

        vbox = QtWidgets.QVBoxLayout(self)

        self.search = QtWidgets.QLineEdit(self)
        self.search.setPlaceholderText("Поиск…")
        vbox.addWidget(self.search)

        self.list = QtWidgets.QListWidget(self)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)  # type: ignore[attr-defined]
        self.list.setUniformItemSizes(True)
        vbox.addWidget(self.list, 1)

        hint = QtWidgets.QLabel(
            "Enter — запуск, Esc — отмена, двойной щелчок — запуск", self
        )
        hint.setStyleSheet("color: gray;")
        vbox.addWidget(hint)

        # Сигналы
        self.search.textChanged.connect(self._refresh)  # type: ignore[attr-defined]
        self.list.itemActivated.connect(
            self._accept
        )  # Enter/двойной щелчок  # type: ignore[attr-defined]

        # Горячие клавиши
        QtWidgets.QShortcut(QtGui.QKeySequence("Escape"), self, activated=self.reject)  # type: ignore[attr-defined]
        QtWidgets.QShortcut(QtGui.QKeySequence("Return"), self, activated=self._accept)  # type: ignore[attr-defined]
        QtWidgets.QShortcut(QtGui.QKeySequence("Enter"), self, activated=self._accept)  # type: ignore[attr-defined]

        # Инициализация
        self.resize(420, 360)
        self._refresh()
        self.search.setFocus(QtCore.Qt.OtherFocusReason)  # type: ignore[attr-defined]

        # Позиционирование у курсора
        try:
            pos = QtGui.QCursor.pos()  # type: ignore[attr-defined]
            self.move(pos.x(), pos.y())
        except Exception:
            pass

    def _refresh(self) -> None:
        q = self.search.text().strip().lower()
        self.list.clear()
        self._view_indices.clear()
        for idx, it in enumerate(self._items):
            if q and q not in it.label.lower():
                continue
            self._view_indices.append(idx)
            self.list.addItem(it.label)
        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def _accept(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._view_indices):
            self._chosen = None
        else:
            true_idx = self._view_indices[row]
            self._chosen = self._items[true_idx].meta
        self.accept()

    def result_meta(self) -> Optional[ScriptMeta]:
        return self._chosen


def show_popup(candidates: List[ScriptMeta]) -> Optional[ScriptMeta]:
    """
    Показывает компактное окно выбора скрипта рядом с курсором.
    Возвращает выбранный ScriptMeta или None, если отменено.

    Если PySide6 недоступен — возбуждает RuntimeError.
    """
    if QtWidgets is None:
        raise RuntimeError("Для popup-меню требуется PySide6 (pip install PySide6)")

    # Гарантируем наличие QApplication
    app_created = False
    app = QtWidgets.QApplication.instance()  # type: ignore[attr-defined]
    if app is None:
        app = QtWidgets.QApplication([])  # type: ignore[call-arg,assignment]
        app_created = True

    items: List[_Item] = []
    for m in sorted(
        candidates, key=lambda x: ((x.group or "").lower(), x.name.lower())
    ):
        label = f"{(m.group + ' / ') if m.group else ''}{m.name}"
        items.append(_Item(label=label, meta=m))

    dlg = _PopupDialog(items)
    rc = dlg.exec()
    chosen = dlg.result_meta()

    if app_created:
        # Аккуратно завершим локально созданный QApp
        try:
            app.quit()  # type: ignore[attr-defined]
        except Exception:
            pass

    if rc == QtWidgets.QDialog.Accepted:  # type: ignore[attr-defined]
        return chosen
    return None


__all__ = [
    "show_popup",
]
