from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ...core.config import Config, EnvMode, default_script_dirs

try:
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
except Exception as e:  # pragma: no cover
    QtCore = None  # type: ignore
    QtGui = None  # type: ignore
    QtWidgets = None  # type: ignore


class SettingsDialog(QtWidgets.QDialog):  # type: ignore[misc]
    """
    Простое окно настроек PContext:
      - каталоги со скриптами (список, добавить/удалить),
      - режим окружений (cached/per-script),
      - авто-открытие результатов (галочка),
      - pip index/extra/proxy (минимально).
    Возвращает новый Config через accept(); get_config().

    Использование:
      dlg = SettingsDialog(cfg, parent=self)
      if dlg.exec() == QtWidgets.QDialog.Accepted:
          new_cfg = dlg.get_config()
          save_config(new_cfg)
    """

    def __init__(self, cfg: Config, parent=None) -> None:
        if QtWidgets is None:
            raise RuntimeError(
                "Требуется PySide6 для SettingsDialog (pip install PySide6)"
            )
        super().__init__(parent)
        self.setWindowTitle("PContext — Настройки")
        self.setModal(True)
        self.resize(560, 460)

        self._cfg = cfg

        main = QtWidgets.QVBoxLayout(self)

        # Tabs
        tabs = QtWidgets.QTabWidget(self)
        main.addWidget(tabs, 1)

        # --- Tab: Скрипты
        tab_scripts = QtWidgets.QWidget(self)
        tabs.addTab(tab_scripts, "Скрипты")
        v1 = QtWidgets.QVBoxLayout(tab_scripts)

        self.list_dirs = QtWidgets.QListWidget(self)
        self.list_dirs.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)  # type: ignore[attr-defined]
        v1.addWidget(self.list_dirs, 1)

        hb_dirs = QtWidgets.QHBoxLayout()
        btn_add = QtWidgets.QPushButton("Добавить…", self)
        btn_remove = QtWidgets.QPushButton("Удалить", self)
        btn_reset = QtWidgets.QPushButton("Сброс по умолчанию", self)
        hb_dirs.addWidget(btn_add)
        hb_dirs.addWidget(btn_remove)
        hb_dirs.addWidget(btn_reset)
        hb_dirs.addStretch(1)
        v1.addLayout(hb_dirs)

        # --- Tab: Окружения
        tab_env = QtWidgets.QWidget(self)
        tabs.addTab(tab_env, "Окружения")
        v2 = QtWidgets.QVBoxLayout(tab_env)

        form_env = QtWidgets.QFormLayout()
        self.combo_env = QtWidgets.QComboBox(self)
        self.combo_env.addItems([EnvMode.CACHED.value, EnvMode.PER_SCRIPT.value])
        form_env.addRow("Режим окружений:", self.combo_env)

        self.chk_auto_open = QtWidgets.QCheckBox(
            "Авто-открывать результаты скриптов", self
        )
        form_env.addRow("", self.chk_auto_open)

        v2.addLayout(form_env)
        v2.addStretch(1)

        # --- Tab: Pip
        tab_pip = QtWidgets.QWidget(self)
        tabs.addTab(tab_pip, "Pip")
        v3 = QtWidgets.QVBoxLayout(tab_pip)

        form_pip = QtWidgets.QFormLayout()
        self.ed_index = QtWidgets.QLineEdit(self)
        self.ed_extra = QtWidgets.QPlainTextEdit(self)  # по одной ссылке на строку
        self.ed_proxy = QtWidgets.QLineEdit(self)

        form_pip.addRow("Index URL:", self.ed_index)
        form_pip.addRow("Extra Index URLs:", self.ed_extra)
        form_pip.addRow("Proxy:", self.ed_proxy)

        v3.addLayout(form_pip)
        v3.addStretch(1)

        # --- Buttons
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, self)  # type: ignore[attr-defined]
        main.addWidget(btns)

        # Signals
        btn_add.clicked.connect(self._on_add_dir)  # type: ignore[attr-defined]
        btn_remove.clicked.connect(self._on_remove_dir)  # type: ignore[attr-defined]
        btn_reset.clicked.connect(self._on_reset_dirs)  # type: ignore[attr-defined]
        btns.accepted.connect(self.accept)  # type: ignore[attr-defined]
        btns.rejected.connect(self.reject)  # type: ignore[attr-defined]

        # Fill from cfg
        self._load_from_config(cfg)

    # -----------------------------
    # Инициализация из Config
    # -----------------------------

    def _load_from_config(self, cfg: Config) -> None:
        # dirs
        self.list_dirs.clear()
        dirs = cfg.script_dirs or default_script_dirs()
        for p in dirs:
            self.list_dirs.addItem(str(Path(p).expanduser()))
        # env mode
        idx = self.combo_env.findText(cfg.env_mode.value)
        self.combo_env.setCurrentIndex(idx if idx >= 0 else 0)
        # auto open
        self.chk_auto_open.setChecked(bool(cfg.auto_open_result))
        # pip
        self.ed_index.setText(cfg.pip_index_url or "")
        self.ed_extra.setPlainText("\n".join(cfg.pip_extra_index_urls or []))
        self.ed_proxy.setText(cfg.pip_proxy or "")

    # -----------------------------
    # Получение нового Config
    # -----------------------------

    def get_config(self) -> Config:
        new = Config()
        # dirs
        dirs: List[Path] = []
        for i in range(self.list_dirs.count()):
            t = self.list_dirs.item(i).text().strip()
            if t:
                dirs.append(Path(t).expanduser())
        new.script_dirs = dirs or default_script_dirs()
        # env
        try:
            new.env_mode = EnvMode(self.combo_env.currentText().strip())
        except Exception:
            new.env_mode = EnvMode.CACHED
        new.auto_open_result = bool(self.chk_auto_open.isChecked())
        # pip
        idx = self.ed_index.text().strip()
        new.pip_index_url = idx or None
        extra_raw = self.ed_extra.toPlainText().strip()
        new.pip_extra_index_urls = [
            ln.strip() for ln in extra_raw.splitlines() if ln.strip()
        ]
        px = self.ed_proxy.text().strip()
        new.pip_proxy = px or None
        # locks — оставляем значение по умолчанию; редактирование можно добавить позже
        new.locks_concurrency = dict(self._cfg.locks_concurrency or {})
        return new

    # -----------------------------
    # Обработчики
    # -----------------------------

    def _on_add_dir(self) -> None:
        # Выбор каталога
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Добавить каталог скриптов")  # type: ignore[attr-defined]
        if not path:
            return
        # Проверим, что такого пути нет
        for i in range(self.list_dirs.count()):
            if (
                Path(self.list_dirs.item(i).text()).expanduser()
                == Path(path).expanduser()
            ):
                return
        self.list_dirs.addItem(path)

    def _on_remove_dir(self) -> None:
        row = self.list_dirs.currentRow()
        if row >= 0:
            self.list_dirs.takeItem(row)

    def _on_reset_dirs(self) -> None:
        self.list_dirs.clear()
        for p in default_script_dirs():
            self.list_dirs.addItem(str(p))
