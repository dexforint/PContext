from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...core.params import ParamProfiles, coerce_all_params
from ...core.types import ParamType, ParameterSpec, ScriptMeta

# PySide6 — опционально. Если отсутствует, бросим понятную ошибку при создании диалога.
try:
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
except Exception as e:  # pragma: no cover
    QtCore = None  # type: ignore
    QtGui = None  # type: ignore
    QtWidgets = None  # type: ignore


@dataclass
class _Field:
    name: str
    spec: ParameterSpec
    widget: QtWidgets.QWidget  # type: ignore[name-defined]
    label: QtWidgets.QLabel  # type: ignore[name-defined]
    # доп. ссылки на вложенные виджеты
    extra: Dict[str, Any]


class ParamsDialog(QtWidgets.QDialog):  # type: ignore[misc]
    """
    Диалог редактирования параметров скрипта + управление профилями.
    Скрипт не запускает. Возвращает значения через get_values() после accept().

    Использование:
      dlg = ParamsDialog(meta, initial_params, parent=self)
      if dlg.exec() == QtWidgets.QDialog.Accepted:
          values = dlg.get_values()
    """

    def __init__(
        self,
        meta: ScriptMeta,
        initial_values: Optional[Dict[str, Any]] = None,
        parent=None,
    ) -> None:
        if QtWidgets is None:
            raise RuntimeError(
                "Для ParamsDialog требуется PySide6 (pip install PySide6)"
            )
        super().__init__(parent)
        self.setWindowTitle(f"PContext — Параметры: {meta.name}")
        self.resize(560, 600)
        self.setModal(True)

        self.meta = meta
        self._profiles = ParamProfiles(meta.stable_id)

        # Итоговые значения после accept()
        self._final_values: Optional[Dict[str, Any]] = None

        main = QtWidgets.QVBoxLayout(self)

        # Верхняя панель: профили
        prof_box = QtWidgets.QGroupBox("Профили", self)
        main.addWidget(prof_box)
        prof_layout = QtWidgets.QHBoxLayout(prof_box)

        self.cmb_profiles = QtWidgets.QComboBox(self)
        self._reload_profiles()
        prof_layout.addWidget(QtWidgets.QLabel("Профиль:", self))
        prof_layout.addWidget(self.cmb_profiles, 1)

        self.btn_prof_use = QtWidgets.QPushButton("Загрузить", self)
        self.btn_prof_save = QtWidgets.QPushButton("Сохранить", self)
        self.btn_prof_save_as = QtWidgets.QPushButton("Сохранить как…", self)
        self.btn_prof_del = QtWidgets.QPushButton("Удалить", self)
        prof_layout.addWidget(self.btn_prof_use)
        prof_layout.addWidget(self.btn_prof_save)
        prof_layout.addWidget(self.btn_prof_save_as)
        prof_layout.addWidget(self.btn_prof_del)

        # Область с параметрами (скролл)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        main.addWidget(scroll, 1)

        container = QtWidgets.QWidget(self)
        scroll.setWidget(container)
        self.form = QtWidgets.QFormLayout(container)
        self.form.setLabelAlignment(QtCore.Qt.AlignLeft)  # type: ignore[attr-defined]
        self.form.setFieldGrowthPolicy(QtWidgets.QFormLayout.ExpandingFieldsGrow)  # type: ignore[attr-defined]

        # Кнопки OK/Cancel
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, self)  # type: ignore[attr-defined]
        main.addWidget(btns)

        # Поля редактора по метаданным
        self._fields: List[_Field] = []
        # Значения по умолчанию + initial + активный профиль (при наличии)
        merged_initial = dict(initial_values or {})
        # Если у профилей есть last_used — применим его поверх initial
        last = self._profiles.get_last_used()
        if last:
            prof_vals = self._profiles.get(last) or {}
            merged_initial = {**merged_initial, **prof_vals}

        self._build_fields(merged_initial)

        # Связи
        self.btn_prof_use.clicked.connect(self._on_profile_use)  # type: ignore[attr-defined]
        self.btn_prof_save.clicked.connect(self._on_profile_save)  # type: ignore[attr-defined]
        self.btn_prof_save_as.clicked.connect(self._on_profile_save_as)  # type: ignore[attr-defined]
        self.btn_prof_del.clicked.connect(self._on_profile_delete)  # type: ignore[attr-defined]

        btns.accepted.connect(self._on_accept)  # type: ignore[attr-defined]
        btns.rejected.connect(self.reject)  # type: ignore[attr-defined]

    # -----------------------------
    # Построение формы
    # -----------------------------

    def _build_fields(self, initial_vals: Dict[str, Any]) -> None:
        # Очищаем форму
        while self.form.rowCount():
            self.form.removeRow(0)
        self._fields.clear()

        # Проходим по параметрам
        for pname, pspec in (self.meta.params or {}).items():
            if pspec.hidden:
                # Скрытые параметры в UI не показываем
                continue
            label = QtWidgets.QLabel(pspec.title or pname, self)
            label.setToolTip(pspec.description or "")

            w, extra = self._create_editor(
                pspec, initial_vals.get(pname, pspec.default)
            )
            self.form.addRow(label, w)
            self._fields.append(
                _Field(name=pname, spec=pspec, widget=w, label=label, extra=extra)
            )

        if not self._fields:
            # Пустая форма
            label = QtWidgets.QLabel("Нет настраиваемых параметров.", self)
            label.setStyleSheet("color: gray;")
            self.form.addRow(label)

    def _create_editor(self, spec: ParameterSpec, value: Any) -> Tuple[QtWidgets.QWidget, Dict[str, Any]]:  # type: ignore[name-defined]
        t = spec.type
        extra: Dict[str, Any] = {}

        if t is ParamType.BOOL:
            cb = QtWidgets.QCheckBox(self)
            cb.setChecked(bool(value if value is not None else spec.default))
            return cb, extra

        if t is ParamType.INT:
            sp = QtWidgets.QSpinBox(self)
            sp.setRange(
                int(spec.min if spec.min is not None else -1_000_000_000),
                int(spec.max if spec.max is not None else 1_000_000_000),
            )
            if spec.step is not None:
                try:
                    sp.setSingleStep(int(spec.step))
                except Exception:
                    pass
            if value is None:
                value = spec.default
            try:
                sp.setValue(int(value if value is not None else 0))
            except Exception:
                pass
            return sp, extra

        if t is ParamType.FLOAT or t is ParamType.SLIDER:
            ds = QtWidgets.QDoubleSpinBox(self)
            ds.setDecimals(6)
            ds.setRange(
                float(spec.min if spec.min is not None else -1e12),
                float(spec.max if spec.max is not None else 1e12),
            )
            if spec.step is not None:
                try:
                    ds.setSingleStep(float(spec.step))
                except Exception:
                    pass
            if value is None:
                value = spec.default
            try:
                ds.setValue(float(value if value is not None else 0.0))
            except Exception:
                pass
            return ds, extra

        if (
            t is ParamType.STR
            or t is ParamType.SECRET
            or t is ParamType.FILE
            or t is ParamType.FOLDER
        ):
            hb = QtWidgets.QHBoxLayout()
            hb.setContentsMargins(0, 0, 0, 0)
            w = QtWidgets.QWidget(self)
            w.setLayout(hb)

            le = QtWidgets.QLineEdit(self)
            le.setPlaceholderText(spec.placeholder or "")
            if value is None:
                value = spec.default
            if value is not None:
                le.setText(str(value))
            hb.addWidget(le, 1)
            extra["line"] = le

            if t is ParamType.SECRET:
                le.setEchoMode(QtWidgets.QLineEdit.Password)  # type: ignore[attr-defined]

            if t is ParamType.FILE:
                btn = QtWidgets.QPushButton("…", self)
                btn.setFixedWidth(30)
                hb.addWidget(btn)

                def choose_file() -> None:
                    filt = spec.file_filter or "All Files (*.*)"
                    path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Выбрать файл", "", filt)  # type: ignore[attr-defined]
                    if path:
                        le.setText(path)

                btn.clicked.connect(choose_file)  # type: ignore[attr-defined]

            if t is ParamType.FOLDER:
                btn = QtWidgets.QPushButton("…", self)
                btn.setFixedWidth(30)
                hb.addWidget(btn)

                def choose_dir() -> None:
                    path = QtWidgets.QFileDialog.getExistingDirectory(self, "Выбрать папку")  # type: ignore[attr-defined]
                    if path:
                        le.setText(path)

                btn.clicked.connect(choose_dir)  # type: ignore[attr-defined]

            return w, extra

        if t is ParamType.ENUM:
            cb = QtWidgets.QComboBox(self)
            opts = list(spec.options or [])
            cb.addItems(opts)
            if value is None:
                value = spec.default
            if isinstance(value, str) and value in opts:
                cb.setCurrentText(value)
            return cb, extra

        if t is ParamType.TEXT:
            ed = QtWidgets.QPlainTextEdit(self)
            ed.setPlaceholderText(spec.placeholder or "")
            if value is None:
                value = spec.default
            if value is not None:
                ed.setPlainText(str(value))
            ed.setMinimumHeight(120)
            return ed, extra

        if t is ParamType.LIST_STR or t is ParamType.LIST_INT:
            ed = QtWidgets.QPlainTextEdit(self)
            ed.setPlaceholderText("По одному значению на строку")
            lines: List[str] = []
            if value is None:
                value = spec.default
            if isinstance(value, list):
                lines = [str(x) for x in value]
            elif isinstance(value, str):
                lines = [ln.strip() for ln in value.splitlines()]
            ed.setPlainText("\n".join(lines))
            ed.setMinimumHeight(100)
            return ed, extra

        if t is ParamType.DICT:
            ed = QtWidgets.QPlainTextEdit(self)
            ed.setPlaceholderText('{"key": "value"}')
            if value is None:
                value = spec.default
            try:
                if isinstance(value, dict):
                    ed.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
                elif isinstance(value, str) and value.strip():
                    # пытаемся отформатировать
                    d = json.loads(value)
                    ed.setPlainText(json.dumps(d, ensure_ascii=False, indent=2))
            except Exception:
                if isinstance(value, str):
                    ed.setPlainText(value)
            ed.setMinimumHeight(140)
            return ed, extra

        # Фолбэк — строка
        le = QtWidgets.QLineEdit(self)
        if value is None:
            value = spec.default
        if value is not None:
            le.setText(str(value))
        return le, extra

    # -----------------------------
    # Чтение значений из формы
    # -----------------------------

    def _read_value(self, field: _Field) -> Any:
        t = field.spec.type
        w = field.widget

        if t is ParamType.BOOL:
            return bool(w.isChecked())  # type: ignore[attr-defined]

        if t is ParamType.INT:
            return int(w.value())  # type: ignore[attr-defined]

        if t is ParamType.FLOAT or t is ParamType.SLIDER:
            return float(w.value())  # type: ignore[attr-defined]

        if (
            t is ParamType.STR
            or t is ParamType.SECRET
            or t is ParamType.FILE
            or t is ParamType.FOLDER
        ):
            le = field.extra.get("line")
            return str(le.text()) if le is not None else str(w.text())  # type: ignore[attr-defined]

        if t is ParamType.ENUM:
            return str(w.currentText())  # type: ignore[attr-defined]

        if t is ParamType.TEXT:
            return str(w.toPlainText())  # type: ignore[attr-defined]

        if t is ParamType.LIST_STR:
            txt = str(w.toPlainText())  # type: ignore[attr-defined]
            vals = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            return vals

        if t is ParamType.LIST_INT:
            txt = str(w.toPlainText())  # type: ignore[attr-defined]
            vals: List[int] = []
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    vals.append(int(ln))
                except Exception:
                    # оставим строку, а валидация coerce_all_params отловит
                    vals.append(int(ln))  # это все равно бросит — передадим дальше
            return vals

        if t is ParamType.DICT:
            txt = str(w.toPlainText())  # type: ignore[attr-defined]
            txt = txt.strip()
            if not txt:
                return {}
            try:
                d = json.loads(txt)
                if isinstance(d, dict):
                    return d
                # пусть coerce_all_params ругнется
                return d
            except json.JSONDecodeError:
                # передадим строку — coerce_all_params преобразует или выбросит ошибку
                return txt

        # Фолбэк — строка
        try:
            return str(w.text())  # type: ignore[attr-defined]
        except Exception:
            return None

    def _collect_values(self) -> Dict[str, Any]:
        vals: Dict[str, Any] = {}
        # Начнем с дефолтов (включая hidden)
        for pname, pspec in (self.meta.params or {}).items():
            vals[pname] = pspec.default
        # Поверх — то, что пользователь ввел для видимых
        for f in self._fields:
            vals[f.name] = self._read_value(f)
        return vals

    def get_values(self) -> Optional[Dict[str, Any]]:
        """
        Возвращает приведенные к типам значения (или None, если диалог отменен).
        """
        return dict(self._final_values) if self._final_values is not None else None

    # -----------------------------
    # Профили
    # -----------------------------

    def _reload_profiles(self) -> None:
        self.cmb_profiles.clear()
        names = self._profiles.list_profiles()
        if not names:
            self.cmb_profiles.addItem("(нет)")
            self.cmb_profiles.setEnabled(False)
        else:
            self.cmb_profiles.setEnabled(True)
            for n in names:
                self.cmb_profiles.addItem(n)
            last = self._profiles.get_last_used()
            if last and last in names:
                idx = self.cmb_profiles.findText(last)
                if idx >= 0:
                    self.cmb_profiles.setCurrentIndex(idx)

    def _on_profile_use(self) -> None:
        name = self.cmb_profiles.currentText().strip()
        if not name or name == "(нет)":
            return
        vals = self._profiles.get(name) or {}
        # Перестроим форму с новыми значениями
        self._build_fields(vals)
        self._profiles.set_last_used(name)

    def _on_profile_save(self) -> None:
        name = self.cmb_profiles.currentText().strip()
        if not name or name == "(нет)":
            # Если профилей нет — предложим "default"
            name = "default"
        vals = self._collect_values()
        # Сохраняем в профиле как есть (без приведения), чтобы не терять форматирование текстов
        self._profiles.set(name, vals, make_default_last=True)
        self._reload_profiles()
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), f"Сохранено в профиле: {name}")  # type: ignore[attr-defined]

    def _on_profile_save_as(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, "Сохранить профиль", "Имя профиля:")  # type: ignore[attr-defined]
        if not ok or not name.strip():
            return
        vals = self._collect_values()
        self._profiles.set(name.strip(), vals, make_default_last=True)
        self._reload_profiles()

    def _on_profile_delete(self) -> None:
        name = self.cmb_profiles.currentText().strip()
        if not name or name == "(нет)":
            return
        ok = self._profiles.delete(name)
        if ok:
            self._reload_profiles()

    # -----------------------------
    # Accept/Validate
    # -----------------------------

    def _on_accept(self) -> None:
        try:
            # Применяем coerce_all_params, чтобы получить гарантированно корректные типы
            raw = self._collect_values()
            final = coerce_all_params(self.meta, raw)
            self._final_values = final
            self.accept()
        except Exception as e:
            # Покажем компактную ошибку
            QtWidgets.QMessageBox.critical(self, "Ошибка параметров", str(e))  # type: ignore[attr-defined]


def edit_script_params(
    meta: ScriptMeta, initial_values: Optional[Dict[str, Any]] = None, parent=None
) -> Optional[Dict[str, Any]]:
    """
    Функция-обертка: открывает диалог параметров и возвращает приведенные значения или None.
    """
    if QtWidgets is None:
        raise RuntimeError(
            "Для окна параметров требуется PySide6 (pip install PySide6)"
        )
    dlg = ParamsDialog(meta, initial_values=initial_values, parent=parent)
    rc = dlg.exec()
    return dlg.get_values() if rc == QtWidgets.QDialog.Accepted else None


__all__ = [
    "ParamsDialog",
    "edit_script_params",
]
