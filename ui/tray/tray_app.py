from __future__ import annotations

import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from ...core.config import (
    Config,
    EnvMode,
    config_dir,
    default_script_dirs,
    ensure_app_dirs,
    load_config,
    logs_dir,
    save_config,
)
from ...core.registry import ScriptRegistry
from ...core.results import handle_result
from ...core.runner import RunOptions, run_one_shot
from ...core.params import ParamProfiles
from ...core.types import Input, InputKind, Scope, ScriptMeta, ScriptType
from ...os_integration.common.shell_open import open_path

# UI
try:
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
except Exception as e:  # pragma: no cover
    QtCore = None  # type: ignore
    QtGui = None  # type: ignore
    QtWidgets = None  # type: ignore

# Доп. окна трея
try:
    from .settings_dialog import SettingsDialog  # type: ignore
    from .params_dialog import edit_script_params  # type: ignore
    from .tasks_view import TasksView  # type: ignore
    from .services_view import ServicesView  # type: ignore
except Exception:
    SettingsDialog = None  # type: ignore
    edit_script_params = None  # type: ignore
    TasksView = None  # type: ignore
    ServicesView = None  # type: ignore

# Наблюдатель за директориями скриптов (через watchdog, если установлен)
try:
    from .dir_watch import DirWatchController  # type: ignore
except Exception:
    DirWatchController = None  # type: ignore

# Менеджер сервисов
try:
    from ...core.services import ServiceManager  # type: ignore
except Exception:
    ServiceManager = None  # type: ignore


def _icon_path() -> Optional[str]:
    try:
        root = Path(__file__).resolve().parents[2]
        png = root / "resources" / "icons" / "pcontext.png"
        if png.exists():
            return str(png)
    except Exception:
        pass
    return None


class TrayApp(QtWidgets.QSystemTrayIcon):  # type: ignore[misc]
    """
    Трей-приложение PContext:
      - доступ к каталогам (скрипты, логи, конфиг),
      - перескан скриптов, статистика,
      - быстрый запуск background-скриптов,
      - управление сервисами (старт/стоп) + окно сервисов,
      - окно задач (oneshot/сервисы),
      - окно настроек,
      - авто-перескан каталогов со скриптами (watchdog при наличии).
    """

    def __init__(self, app: QtWidgets.QApplication) -> None:  # type: ignore[name-defined]
        if QtWidgets is None:
            raise RuntimeError("Требуется PySide6 для работы трея: pip install PySide6")

        self.app = app
        ensure_app_dirs()
        self.cfg: Config = load_config(create_if_missing=True)

        # Менеджер скриптов
        self.registry = ScriptRegistry(script_dirs=self.cfg.script_dirs)

        # Менеджер сервисов (используется в меню «Сервисы» и окне)
        if ServiceManager is None:
            raise RuntimeError("Не найден ServiceManager. Проверьте установку пакета.")
        self._service_manager = ServiceManager(env_mode=self.cfg.env_mode)

        # Окна
        self._tasks_win: Optional[QtWidgets.QWidget] = None  # type: ignore[assignment]
        self._services_win: Optional[QtWidgets.QWidget] = None  # type: ignore[assignment]

        # Иконка
        icon = QtGui.QIcon(_icon_path() or "")
        super().__init__(icon)
        self.setToolTip("PContext")

        # Меню
        self.menu = QtWidgets.QMenu()
        self.setContextMenu(self.menu)

        # Таймер для обновления подменю «Сервисы»/«Скрипты (фон)»
        self._services_timer = QtCore.QTimer()
        self._services_timer.setInterval(2000)  # 2s
        self._services_timer.timeout.connect(self._rebuild_menu_async)  # type: ignore[attr-defined]

        # Клик ЛКМ — открыть меню у курсора
        self.activated.connect(self._on_activated)  # type: ignore[attr-defined]

        # Вочер каталогов
        self._dirwatch = None
        self._try_start_dirwatch()

        # Первичная сборка меню
        self._rebuild_menu()

        # Покажем иконку
        self.show()
        self._services_timer.start()

        # Перескан скриптов в фоне
        threading.Thread(
            target=self._rescan_in_background, name="PCTX-Rescan", daemon=True
        ).start()

    # -----------------------------
    # Директории скриптов: watcher
    # -----------------------------

    def _try_start_dirwatch(self) -> None:
        if DirWatchController is None:
            self._dirwatch = None
            return
        try:
            self._dirwatch = DirWatchController(
                self.cfg.script_dirs,
                on_changed=self._on_scripts_changed,
                debounce_ms=500,
            )
            active = self._dirwatch.start()
            if not active:
                self._dirwatch = None
        except Exception:
            self._dirwatch = None

    def _restart_dirwatch(self) -> None:
        try:
            if self._dirwatch is not None:
                self._dirwatch.stop()
        except Exception:
            pass
        self._dirwatch = None
        self._try_start_dirwatch()

    def _on_scripts_changed(self) -> None:
        # Срабатывает из фонового треда watchdog — инициируем перескан в фоне
        threading.Thread(
            target=self._rescan_in_background, name="PCTX-Rescan-Change", daemon=True
        ).start()

    # -----------------------------
    # Меню
    # -----------------------------

    def _rebuild_menu_async(self) -> None:
        self._rebuild_menu()

    def _rebuild_menu(self) -> None:
        m = QtWidgets.QMenu()

        title = m.addAction("PContext")
        title.setEnabled(False)

        # Каталоги
        m.addSeparator()
        act_scripts = m.addAction("Открыть папку скриптов")
        act_scripts.triggered.connect(self._act_open_scripts)  # type: ignore[attr-defined]

        act_logs = m.addAction("Открыть папку логов")
        act_logs.triggered.connect(self._act_open_logs)  # type: ignore[attr-defined]

        act_cfg = m.addAction("Открыть конфигурацию")
        act_cfg.triggered.connect(self._act_open_config)  # type: ignore[attr-defined]

        # Скрипты/перескан
        m.addSeparator()
        act_rescan = m.addAction("Пересканировать скрипты")
        act_rescan.triggered.connect(self._act_rescan_scripts)  # type: ignore[attr-defined]

        count = self.registry.count()
        info = m.addAction(f"Скриптов: {count}")
        info.setEnabled(False)

        # Быстрый запуск background-скриптов
        m.addSeparator()
        bg_menu = m.addMenu("Скрипты (фон)")
        self._fill_background_scripts_menu(bg_menu)

        # Сервисы (Start/Stop)
        m.addSeparator()
        svc_menu = m.addMenu("Сервисы")
        self._fill_services_menu(svc_menu)

        # Окна: Задачи/Сервисы/Настройки
        m.addSeparator()
        act_tasks = m.addAction("Задачи…")
        act_tasks.triggered.connect(self._act_open_tasks_window)  # type: ignore[attr-defined]

        act_svcwin = m.addAction("Сервисы…")
        act_svcwin.triggered.connect(self._act_open_services_window)  # type: ignore[attr-defined]

        act_settings = m.addAction("Настройки…")
        act_settings.triggered.connect(self._act_open_settings)  # type: ignore[attr-defined]

        # Выход
        m.addSeparator()
        act_exit = m.addAction("Выход")
        act_exit.triggered.connect(self._act_exit)  # type: ignore[attr-defined]

        self.setContextMenu(m)
        self.menu = m

    def _fill_services_menu(self, menu: QtWidgets.QMenu) -> None:  # type: ignore[name-defined]
        menu.clear()

        # Список всех сервисных скриптов
        entries = self.registry.list_entries()
        svc_scripts: List[ScriptMeta] = [
            e.meta for e in entries if e.meta.type is ScriptType.SERVICE
        ]

        if not svc_scripts:
            a = menu.addAction("Нет сервисных скриптов")
            a.setEnabled(False)
        else:
            # Группировка по group
            def ensure_path(
                parent_menu: QtWidgets.QMenu, group_path: Optional[str]
            ) -> QtWidgets.QMenu:
                if not group_path:
                    return parent_menu
                node = parent_menu
                parts = group_path.split("/")
                for part in parts:
                    found = None
                    for act in node.actions():
                        if act.menu() and act.text() == part:
                            found = act.menu()
                            break
                    if found is None:
                        found = node.addMenu(part)
                    node = found
                return node

            for meta in sorted(
                svc_scripts, key=lambda m: ((m.group or "").lower(), m.name.lower())
            ):
                parent = ensure_path(menu, meta.group)
                running = self._service_is_running(meta.stable_id)

                # Заголовок с индикатором состояния
                status_prefix = "● " if running else "○ "
                sub = parent.addMenu(status_prefix + meta.name)

                # Статус
                st = sub.addAction(f"Статус: {'RUNNING' if running else 'STOPPED'}")
                st.setEnabled(False)

                # Запустить
                a_start = sub.addAction("Запустить")
                a_start.setEnabled(not running)
                a_start.triggered.connect(lambda _, mm=meta: self._act_start_service(mm))  # type: ignore[attr-defined]

                # Остановить
                a_stop = sub.addAction("Остановить")
                a_stop.setEnabled(running)
                a_stop.triggered.connect(lambda _, mm=meta: self._act_stop_service(mm))  # type: ignore[attr-defined]

                # Параметры инициализации (хранятся в профилях)
                if edit_script_params is not None and (meta.params or {}):
                    sub.addSeparator()
                    p = sub.addAction("Параметры инициализации…")
                    p.triggered.connect(lambda _, mm=meta: self._act_edit_params(mm))  # type: ignore[attr-defined]

        menu.addSeparator()
        stop_all = menu.addAction("Остановить все запущенные")
        stop_all.triggered.connect(self._act_stop_all_services)  # type: ignore[attr-defined]

        # Окно сервисов
        open_win = menu.addAction("Открыть окно сервисов…")
        open_win.triggered.connect(self._act_open_services_window)  # type: ignore[attr-defined]

    def _fill_background_scripts_menu(self, menu: QtWidgets.QMenu) -> None:  # type: ignore[name-defined]
        menu.clear()
        entries = self.registry.list_entries()
        bg_scripts: List[ScriptMeta] = [
            e.meta
            for e in entries
            if e.meta.type is ScriptType.ONE_SHOT
            and e.meta.accepts.scope is Scope.BACKGROUND
        ]
        if not bg_scripts:
            a = menu.addAction("Нет подходящих скриптов")
            a.setEnabled(False)
            return

        # Группировка по дереву group (Vision/YOLO)
        def ensure_path(
            parent_menu: QtWidgets.QMenu, group_path: Optional[str]
        ) -> QtWidgets.QMenu:
            if not group_path:
                return parent_menu
            node = parent_menu
            parts = group_path.split("/")
            for part in parts:
                found = None
                for act in node.actions():
                    if act.menu() and act.text() == part:
                        found = act.menu()
                        break
                if found is None:
                    found = node.addMenu(part)
                node = found
            return node

        for meta in sorted(
            bg_scripts, key=lambda m: ((m.group or "").lower(), m.name.lower())
        ):
            parent = ensure_path(menu, meta.group)
            sub = parent.addMenu(meta.name)

            run_act = sub.addAction("Запустить")
            run_act.triggered.connect(lambda _, mm=meta: self._act_run_background_script(mm))  # type: ignore[attr-defined]

            params_act = sub.addAction("Параметры…")
            params_act.triggered.connect(lambda _, mm=meta: self._act_edit_params(mm))  # type: ignore[attr-defined]

    # -----------------------------
    # Действия меню
    # -----------------------------

    def _notify(self, title: str, text: str, icon: QtWidgets.QSystemTrayIcon.MessageIcon = QtWidgets.QSystemTrayIcon.Information) -> None:  # type: ignore[name-defined]
        try:
            self.showMessage(title, text, icon, 3500)
        except Exception:
            pass

    def _act_open_scripts(self) -> None:
        dirs = self.cfg.script_dirs or default_script_dirs()
        open_path(dirs[0])

    def _act_open_logs(self) -> None:
        open_path(logs_dir())

    def _act_open_config(self) -> None:
        open_path(config_dir())

    def _act_rescan_scripts(self) -> None:
        entries, errors = self.registry.rescan()
        msg = f"Найдено скриптов: {len(entries)}"
        if errors:
            msg += f"\nОшибок: {len(errors)}"
        self._notify("PContext", msg)
        self._rebuild_menu()

    def _act_open_tasks_window(self) -> None:
        if TasksView is None:
            self._notify("PContext", "Окно задач недоступно (нет зависимости PySide6/psutil).", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
            return
        if self._tasks_win is None or not isinstance(self._tasks_win, QtWidgets.QWidget) or not self._tasks_win.isVisible():  # type: ignore[attr-defined]
            self._tasks_win = TasksView()
        self._tasks_win.show()  # type: ignore[attr-defined]
        self._tasks_win.raise_()  # type: ignore[attr-defined]
        self._tasks_win.activateWindow()  # type: ignore[attr-defined]

    def _act_open_services_window(self) -> None:
        if ServicesView is None:
            self._notify("PContext", "Окно сервисов недоступно (нет зависимости PySide6).", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
            return
        if self._services_win is None or not isinstance(self._services_win, QtWidgets.QWidget) or not self._services_win.isVisible():  # type: ignore[attr-defined]
            self._services_win = ServicesView(self._service_manager)  # type: ignore[call-arg]
        self._services_win.show()  # type: ignore[attr-defined]
        self._services_win.raise_()  # type: ignore[attr-defined]
        self._services_win.activateWindow()  # type: ignore[attr-defined]

    def _act_open_settings(self) -> None:
        if SettingsDialog is None:
            self._notify("PContext", "Окно настроек недоступно (нет зависимости PySide6).", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
            return
        dlg = SettingsDialog(self.cfg)
        if dlg.exec() == QtWidgets.QDialog.Accepted:  # type: ignore[attr-defined]
            new_cfg = dlg.get_config()
            save_config(new_cfg)
            # Применим изменения: пути/режим окружений/автооткрытие
            self.cfg = new_cfg
            self.registry = ScriptRegistry(script_dirs=self.cfg.script_dirs)
            # Обновим сервис-менеджер режим окружений
            self._service_manager.env_mode = self.cfg.env_mode
            # Перезапустим watcher при изменении директорий
            self._restart_dirwatch()
            self._notify("PContext", "Настройки применены.")
            self._rebuild_menu()

    def _act_edit_params(self, meta: ScriptMeta) -> None:
        if edit_script_params is None:
            self._notify("PContext", "Окно параметров недоступно (нет зависимости PySide6).", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
            return
        profiles = ParamProfiles(meta.stable_id)
        last = profiles.get_last_used() or "default"
        initial = profiles.get(last) or {}
        vals = edit_script_params(meta, initial_values=initial)
        if vals is not None:
            profiles.set("default", vals, make_default_last=True)
            self._notify("PContext", f"Параметры сохранены (профиль: default).")

    def _act_run_background_script(self, meta: ScriptMeta) -> None:
        threading.Thread(
            target=self._run_bg_script_thread, args=(meta,), daemon=True
        ).start()

    def _act_start_service(self, meta: ScriptMeta) -> None:
        threading.Thread(
            target=self._start_service_thread, args=(meta,), daemon=True
        ).start()

    def _act_stop_service(self, meta: ScriptMeta) -> None:
        threading.Thread(
            target=self._stop_service_thread, args=(meta.stable_id,), daemon=True
        ).start()

    def _act_stop_all_services(self) -> None:
        threading.Thread(target=self._stop_all_services_thread, daemon=True).start()

    # -----------------------------
    # Фоновые операции
    # -----------------------------

    def _run_bg_script_thread(self, meta: ScriptMeta) -> None:
        try:
            # Inputs — background
            inputs = [Input(kind=InputKind.BACKGROUND, path=None, name=None, mime=None)]

            # Параметры — по последнему активному профилю (если есть)
            profiles = ParamProfiles(meta.stable_id)
            last = profiles.get_last_used()
            overrides = profiles.get(last) if last else None

            # Опции запуска
            opts = RunOptions(
                env_mode=(
                    self.cfg.env_mode
                    if isinstance(self.cfg.env_mode, EnvMode)
                    else EnvMode.CACHED
                ),
                timeout_seconds=(
                    meta.timeout.one_shot_seconds
                    if meta.timeout.one_shot_seconds
                    else None
                ),
                cwd=None,
            )

            result = run_one_shot(
                meta=meta,
                inputs=inputs,
                params_overrides=overrides,
                options=opts,
            )

            auto_open = self._auto_open_flag(meta)

            if result.status.name == "OK":
                if auto_open:
                    try:
                        handle_result(result.result, auto_open=True)
                    except Exception:
                        pass
                self._notify(
                    "PContext",
                    f"{meta.name}: выполнено за {int(result.elapsed_seconds)} c",
                )
            else:
                msg = f"{meta.name}: {result.status.value}"
                if result.log_file:
                    msg += f"\nЛог: {result.log_file}"
                self._notify("PContext", msg, QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]

        except Exception as e:
            self._notify("PContext", f"{meta.name}: ошибка запуска ({e})", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]

    def _start_service_thread(self, meta: ScriptMeta) -> None:
        try:
            # Параметры инициализации берём из профиля (если сохранён)
            profiles = ParamProfiles(meta.stable_id)
            last = profiles.get_last_used()
            init_params = profiles.get(last) if last else {}
            handle = self._service_manager.get_or_start(
                meta, init_params=init_params, cwd=None, ready_timeout=120.0
            )
            if handle and handle.is_alive():
                self._notify("PContext", f"Сервис запущен: {meta.name}")
            else:
                self._notify("PContext", f"Не удалось запустить сервис: {meta.name}", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
        except Exception as e:
            self._notify("PContext", f"{meta.name}: ошибка запуска сервиса ({e})", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
        finally:
            QtCore.QTimer.singleShot(0, self._rebuild_menu)  # type: ignore[attr-defined]

    def _stop_service_thread(self, script_id: str) -> None:
        try:
            ok = self._service_manager.shutdown(script_id, grace_timeout=3.0)
            self._notify(
                "PContext", f"{script_id}: {'остановлен' if ok else 'ошибка остановки'}"
            )
        except Exception as e:
            self._notify("PContext", f"{script_id}: ошибка остановки ({e})", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
        finally:
            QtCore.QTimer.singleShot(0, self._rebuild_menu)  # type: ignore[attr-defined]

    def _stop_all_services_thread(self) -> None:
        try:
            self._service_manager.shutdown_all(grace_timeout=3.0)
            self._notify("PContext", "Все сервисы остановлены")
        except Exception as e:
            self._notify("PContext", f"Ошибка остановки всех сервисов: {e}", QtWidgets.QSystemTrayIcon.Warning)  # type: ignore[attr-defined]
        finally:
            QtCore.QTimer.singleShot(0, self._rebuild_menu)  # type: ignore[attr-defined]

    # -----------------------------
    # Вспомогательные
    # -----------------------------

    def _auto_open_flag(self, meta: ScriptMeta) -> bool:
        per_script = (
            True if meta.auto_open_result is None else bool(meta.auto_open_result)
        )
        return bool(per_script and self.cfg.auto_open_result)

    def _service_is_running(self, script_id: str) -> bool:
        try:
            h = self._service_manager.get(script_id)
            return bool(h and h.is_alive())
        except Exception:
            return False

    def _rescan_in_background(self) -> None:
        try:
            self.registry.rescan()
            # После перескана — обновим меню (в главном потоке)
            QtCore.QTimer.singleShot(0, self._rebuild_menu)  # type: ignore[attr-defined]
        except Exception:
            pass

    # -----------------------------
    # Системные события
    # -----------------------------

    def _on_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:  # type: ignore[name-defined]
        if reason == QtWidgets.QSystemTrayIcon.Trigger:  # type: ignore[attr-defined]
            pos = QtGui.QCursor.pos()  # type: ignore[attr-defined]
            self.menu.popup(pos)

    # -----------------------------
    # Завершение
    # -----------------------------

    def __del__(self) -> None:
        try:
            if self._dirwatch is not None:
                self._dirwatch.stop()
        except Exception:
            pass


def main() -> int:
    if QtWidgets is None:
        sys.stderr.write("Ошибка: для трея требуется PySide6 (pip install PySide6)\n")
        return 2
    app = QtWidgets.QApplication(sys.argv)  # type: ignore[name-defined]

    # Иконка приложения
    try:
        ico = _icon_path()
        if ico:
            app.setWindowIcon(QtGui.QIcon(ico))  # type: ignore[attr-defined]
    except Exception:
        pass

    tray = TrayApp(app)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
