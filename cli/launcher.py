from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.config import EnvMode, load_config
from ..core.registry import ScriptRegistry
from ..core.results import handle_result
from ..core.runner import RunOptions, run_one_shot
from ..core.params import ParamProfiles
from ..core.types import Input, InputKind, Scope, ScriptMeta, ScriptType
from ..core.utils import build_inputs, detect_invocation_scope
from ..core.services import ServiceManager


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="pcontext-cli launcher",
        description="PContext launcher (вызов из контекстного меню)",
    )
    ap.add_argument(
        "launcher",
        nargs="?",
        help="внутренний субкомандный префикс (может быть опущен)",
    )
    ap.add_argument(
        "--scope", required=True, help="контекст: files | dirs | background"
    )
    ap.add_argument("--paths", nargs="*", default=[], help="пути выделенных элементов")
    ap.add_argument("--cwd", default=None, help="текущая директория (для background)")
    ap.add_argument(
        "--no-auto-open",
        action="store_true",
        help="не открывать результаты автоматически",
    )
    return ap.parse_args(argv)


def _normalize_scope(token: str) -> Scope:
    t = (token or "").strip().lower()
    if t in ("files", "file"):
        return Scope.FILES
    if t in ("dirs", "directories", "folders", "folder"):
        return Scope.DIRECTORIES
    if t in ("background", "bg", "empty"):
        return Scope.BACKGROUND
    return Scope.MIXED


def _tk_available() -> bool:
    try:
        import tkinter  # noqa

        return True
    except Exception:
        return False


def _qt_available() -> bool:
    try:
        from ..ui.popup.popup_menu import show_popup  # noqa: F401
        from PySide6 import QtWidgets  # noqa: F401

        return True
    except Exception:
        return False


def _choose_script_popup_tk(candidates: List[ScriptMeta]) -> Optional[ScriptMeta]:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return None

    items: List[Tuple[str, ScriptMeta]] = []
    for m in sorted(
        candidates, key=lambda x: ((x.group or "").lower(), x.name.lower())
    ):
        label = f"{(m.group + ' / ') if m.group else ''}{m.name}"
        items.append((label, m))

    chosen: Dict[str, Optional[ScriptMeta]] = {"meta": None}

    root = tk.Tk()
    root.withdraw()

    top = tk.Toplevel(root)
    top.title("PContext — выбрать скрипт")
    top.attributes("-topmost", True)

    try:
        x = top.winfo_pointerx()
        y = top.winfo_pointery()
        top.geometry(f"+{x}+{y}")
    except Exception:
        pass

    search_var = tk.StringVar()
    entry = ttk.Entry(top, textvariable=search_var)
    entry.pack(fill="x", padx=8, pady=(8, 4))

    frame = ttk.Frame(top)
    frame.pack(fill="both", expand=True, padx=8, pady=4)

    scrollbar = ttk.Scrollbar(frame, orient="vertical")
    listbox = tk.Listbox(
        frame, selectmode="browse", yscrollcommand=scrollbar.set, height=14, width=48
    )
    scrollbar.config(command=listbox.yview)
    listbox.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    hint = ttk.Label(top, text="Enter — запуск, Esc — отмена, двойной щелчок — запуск")
    hint.pack(fill="x", padx=8, pady=(4, 8))

    view_indices: List[int] = []

    def refresh() -> None:
        q = search_var.get().strip().lower()
        listbox.delete(0, "end")
        view_indices.clear()
        for idx, (label, _m) in enumerate(items):
            if q and q not in label.lower():
                continue
            view_indices.append(idx)
            listbox.insert("end", label)
        if listbox.size() > 0:
            listbox.selection_set(0)
            listbox.activate(0)

    def do_ok(event=None) -> None:  # type: ignore[override]
        if listbox.size() <= 0:
            top.destroy()
            return
        sel = listbox.curselection()
        if not sel:
            top.destroy()
            return
        view_pos = int(sel[0])
        true_idx = view_indices[view_pos]
        chosen["meta"] = items[true_idx][1]
        top.destroy()

    def do_cancel(event=None) -> None:  # type: ignore[override]
        chosen["meta"] = None
        top.destroy()

    entry.bind("<Return>", do_ok)
    entry.bind("<Escape>", do_cancel)
    listbox.bind("<Return>", do_ok)
    listbox.bind("<Escape>", do_cancel)
    listbox.bind("<Double-Button-1>", do_ok)

    refresh()
    entry.focus_set()
    root.wait_window(top)
    try:
        root.destroy()
    except Exception:
        pass
    return chosen["meta"]


def _choose_script_popup_qt(candidates: List[ScriptMeta]) -> Optional[ScriptMeta]:
    try:
        from ..ui.popup.popup_menu import show_popup

        return show_popup(candidates)
    except Exception:
        return None


def _build_inputs_from_args(
    scope_token: str, paths: List[str], cwd: Optional[str]
) -> List[Input]:
    scope = _normalize_scope(scope_token)
    if scope is Scope.BACKGROUND:
        return [Input(kind=InputKind.BACKGROUND, path=None, name=None, mime=None)]  # type: ignore[name-defined]
    return build_inputs(list(paths or []), scope)


def _find_service_for_proxy(
    chosen: ScriptMeta, registry: ScriptRegistry
) -> Optional[ScriptMeta]:
    if getattr(chosen, "proxy_service", None):
        sid = str(chosen.proxy_service)
        for e in registry.list_entries():
            if e.id == sid and e.meta.type is ScriptType.SERVICE:
                return e.meta
        for e in registry.list_entries():
            if e.meta.id == sid and e.meta.type is ScriptType.SERVICE:
                return e.meta
    deps = list(getattr(chosen.depends, "scripts", []) or [])
    if deps:
        for sid in deps:
            for e in registry.list_entries():
                if (
                    e.id == sid or e.meta.id == sid
                ) and e.meta.type is ScriptType.SERVICE:
                    return e.meta
    return None


from ..core.utils import build_inputs  # ensure import order


def _auto_open_flag(
    meta: ScriptMeta, config_auto_open: bool, cli_no_auto_open: bool
) -> bool:
    if cli_no_auto_open:
        return False
    per_script = True if meta.auto_open_result is None else bool(meta.auto_open_result)
    return bool(per_script and config_auto_open)


def _show_messagebox(title: str, message: str, kind: str = "info") -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        if kind == "error":
            messagebox.showerror(title, message, parent=root)
        elif kind == "warning":
            messagebox.showwarning(title, message, parent=root)
        else:
            messagebox.showinfo(title, message, parent=root)
        root.destroy()
    except Exception:
        import sys as _sys

        _sys.stderr.write(f"{title}: {message}\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ns = _parse_args(argv)
    cfg = load_config(create_if_missing=True)

    inputs = _build_inputs_from_args(ns.scope, ns.paths, ns.cwd)
    _ = detect_invocation_scope(inputs)

    registry = ScriptRegistry(script_dirs=cfg.script_dirs)
    registry.rescan()
    entries = registry.filter_by_inputs(inputs)
    candidates = [e.meta for e in entries]

    if not candidates:
        _show_messagebox(
            "PContext",
            "Нет подходящих скриптов для выбранного контекста.",
            kind="warning",
        )
        return 0

    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen: Optional[ScriptMeta] = None
        if _qt_available():
            chosen = _choose_script_popup_qt(candidates)
        if not chosen and _tk_available():
            chosen = _choose_script_popup_tk(candidates)
        if not chosen:
            chosen = candidates[0]
    if not chosen:
        return 0

    service_meta = None
    if chosen.type is ScriptType.ONE_SHOT:
        service_meta = _find_service_for_proxy(chosen, registry)

    if service_meta is not None:
        try:
            proxy_profiles = ParamProfiles(chosen.stable_id)
            last_proxy = proxy_profiles.get_last_used()
            req_overrides = proxy_profiles.get(last_proxy) if last_proxy else {}

            svc_profiles = ParamProfiles(service_meta.stable_id)
            last_svc = svc_profiles.get_last_used()
            init_params = svc_profiles.get(last_svc) if last_svc else {}

            id_map = {e.id: e.meta for e in registry.list_entries()}

            def resolver(sid: str) -> Optional[ScriptMeta]:
                return id_map.get(sid)

            sm = ServiceManager(env_mode=cfg.env_mode, resolver=resolver)  # type: ignore[arg-type]
            sm.get_or_start(
                service_meta,
                init_params=init_params,
                cwd=Path(ns.cwd) if ns.cwd else None,
                ready_timeout=120.0,
            )

            request_timeout = (
                chosen.timeout.one_shot_seconds
                if chosen.timeout.one_shot_seconds
                else None
            )

            req_entry = getattr(chosen, "proxy_entry", None)
            resp = sm.request(
                service_meta,
                inputs=inputs,
                params_overrides=req_overrides,
                request_timeout=request_timeout,
                cwd=Path(ns.cwd) if ns.cwd else None,
                request_entry=req_entry,
            )

            if not resp.ok:
                msg_lines = [f"Сервис: {service_meta.name}", "Статус: ERROR"]
                if resp.error_type:
                    msg_lines.append(f"Ошибка: {resp.error_type}")
                if resp.error_message:
                    msg_lines.append(resp.error_message)
                _show_messagebox(
                    "PContext — ошибка сервиса", "\n".join(msg_lines), kind="error"
                )
                return 1

            auto_open = _auto_open_flag(chosen, cfg.auto_open_result, ns.no_auto_open)
            if auto_open:
                try:
                    handle_result(resp.result, auto_open=True)
                except Exception:
                    pass
            return 0
        except Exception as e:
            _show_messagebox("PContext — ошибка сервиса", str(e), kind="error")
            return 1

    profiles = ParamProfiles(chosen.stable_id)
    last = profiles.get_last_used()
    overrides: Optional[Dict[str, Any]] = profiles.get(last) if last else None

    cwd_path = Path(ns.cwd) if ns.cwd else None
    opts = RunOptions(
        env_mode=cfg.env_mode if isinstance(cfg.env_mode, EnvMode) else EnvMode.CACHED,
        timeout_seconds=(
            chosen.timeout.one_shot_seconds if chosen.timeout.one_shot_seconds else None
        ),
        cwd=cwd_path,
    )

    result = run_one_shot(
        meta=chosen, inputs=inputs, params_overrides=overrides, options=opts
    )
    auto_open = _auto_open_flag(chosen, cfg.auto_open_result, ns.no_auto_open)

    if result.status.name == "OK":
        if auto_open:
            try:
                handle_result(result.result, auto_open=True)
            except Exception:
                pass
        return 0

    msg_lines = [f"Скрипт: {chosen.name}", f"Статус: {result.status.value}"]
    if result.error_type or result.error_message:
        msg_lines.append("")
        if result.error_type:
            msg_lines.append(f"Ошибка: {result.error_type}")
        if result.error_message:
            msg_lines.append(result.error_message)
    if result.log_file:
        msg_lines.append("")
        msg_lines.append(f"Лог: {result.log_file}")

    _show_messagebox("PContext — ошибка выполнения", "\n".join(msg_lines), kind="error")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
