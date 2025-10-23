from __future__ import annotations

import os
import subprocess
import sys
import time
from ctypes import (
    POINTER,
    WinError,
    byref,
    c_size_t,
    c_void_p,
    c_wchar_p,
    windll,
    wintypes,
)  # type: ignore
from typing import Optional

try:
    import shutil
except Exception:
    shutil = None  # type: ignore

# Опциональная зависимость — если установлена, используем как простой кроссплатформенный бэкенд
try:
    import pyperclip  # type: ignore

    _HAS_PYPERCLIP = True
except Exception:
    pyperclip = None  # type: ignore
    _HAS_PYPERCLIP = False


# -----------------------------
# Вспомогательные утилиты
# -----------------------------


def _which(cmd: str) -> Optional[str]:
    if shutil is None:
        return None
    return shutil.which(cmd)


def _run(
    cmd: list[str], input_text: Optional[str] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        input=input_text.encode("utf-8") if input_text is not None else None,
        capture_output=True,
        check=False,
    )


# -----------------------------
# Windows (ctypes)
# -----------------------------

# Константы WinAPI для буфера обмена
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Типы и функции
OpenClipboard = None
EmptyClipboard = None
CloseClipboard = None
SetClipboardData = None
GetClipboardData = None
IsClipboardFormatAvailable = None

GlobalAlloc = None
GlobalLock = None
GlobalUnlock = None


def _init_winapi() -> None:
    global OpenClipboard, EmptyClipboard, CloseClipboard, SetClipboardData, GetClipboardData
    global IsClipboardFormatAvailable, GlobalAlloc, GlobalLock, GlobalUnlock
    if os.name != "nt":
        return
    user32 = windll.user32
    kernel32 = windll.kernel32

    OpenClipboard = user32.OpenClipboard
    OpenClipboard.argtypes = [wintypes.HWND]
    OpenClipboard.restype = wintypes.BOOL

    EmptyClipboard = user32.EmptyClipboard
    EmptyClipboard.argtypes = []
    EmptyClipboard.restype = wintypes.BOOL

    CloseClipboard = user32.CloseClipboard
    CloseClipboard.argtypes = []
    CloseClipboard.restype = wintypes.BOOL

    SetClipboardData = user32.SetClipboardData
    SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    SetClipboardData.restype = wintypes.HANDLE

    GetClipboardData = user32.GetClipboardData
    GetClipboardData.argtypes = [wintypes.UINT]
    GetClipboardData.restype = wintypes.HANDLE

    IsClipboardFormatAvailable = user32.IsClipboardFormatAvailable
    IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    IsClipboardFormatAvailable.restype = wintypes.BOOL

    GlobalAlloc = kernel32.GlobalAlloc
    GlobalAlloc.argtypes = [wintypes.UINT, c_size_t]
    GlobalAlloc.restype = wintypes.HGLOBAL

    GlobalLock = kernel32.GlobalLock
    GlobalLock.argtypes = [wintypes.HGLOBAL]
    GlobalLock.restype = c_void_p

    GlobalUnlock = kernel32.GlobalUnlock
    GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    GlobalUnlock.restype = wintypes.BOOL


_init_winapi()


def _win_open_clipboard_with_retry(retries: int = 10, delay: float = 0.02) -> None:
    assert OpenClipboard is not None
    for _ in range(retries):
        if OpenClipboard(0):
            return
        time.sleep(delay)
    raise WinError()  # type: ignore[misc]


def _win_set_text(text: str) -> bool:
    assert OpenClipboard and EmptyClipboard and CloseClipboard and SetClipboardData
    assert GlobalAlloc and GlobalLock and GlobalUnlock

    # Windows ожидает UTF-16-LE c нулевым терминатором
    data = text.encode("utf-16le") + b"\x00\x00"
    size = len(data)
    try:
        _win_open_clipboard_with_retry()
        if not EmptyClipboard():
            # Продолжаем — иногда возвращает False, но буфер очищен
            pass
        hglobal = GlobalAlloc(GMEM_MOVEABLE, size)
        if not hglobal:
            raise WinError()  # type: ignore[misc]
        ptr = GlobalLock(hglobal)
        if not ptr:
            raise WinError()  # type: ignore[misc]
        try:
            # Копируем байты в выделенную память
            # memmove(dst, src, size)
            windll.kernel32.RtlMoveMemory(ptr, data, size)  # type: ignore[attr-defined]
        finally:
            GlobalUnlock(hglobal)
        if not SetClipboardData(CF_UNICODETEXT, hglobal):
            # Если SetClipboardData не принял — нам нужно освободить память самостоятельно
            # Но документация говорит, что при успехе ответственность переходит к ОС
            # При провале — можем попытаться освобождать (GlobalFree), но в ctypes нет прототипа — оставим GC
            raise WinError()  # type: ignore[misc]
        return True
    except Exception:
        return False
    finally:
        try:
            CloseClipboard()
        except Exception:
            pass


def _win_get_text() -> Optional[str]:
    assert (
        OpenClipboard
        and CloseClipboard
        and GetClipboardData
        and IsClipboardFormatAvailable
        and GlobalLock
        and GlobalUnlock
    )
    try:
        _win_open_clipboard_with_retry()
        if not IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = GlobalLock(handle)
        if not ptr:
            return None
        try:
            # Читаем нуль-терминированную строку UTF-16LE
            s = None
            try:
                s = c_wchar_p(ptr).value  # type: ignore[arg-type]
            except Exception:
                # Фолбэк — через wstring_at
                from ctypes import wstring_at  # type: ignore

                s = wstring_at(ptr)  # type: ignore[arg-type]
            return s
        finally:
            GlobalUnlock(handle)
    except Exception:
        return None
    finally:
        try:
            CloseClipboard()
        except Exception:
            pass


def _win_clear() -> bool:
    assert OpenClipboard and EmptyClipboard and CloseClipboard
    try:
        _win_open_clipboard_with_retry()
        EmptyClipboard()
        return True
    except Exception:
        return False
    finally:
        try:
            CloseClipboard()
        except Exception:
            pass


# -----------------------------
# macOS (pbcopy/pbpaste)
# -----------------------------


def _mac_set_text(text: str) -> bool:
    try:
        res = _run(["pbcopy"], input_text=text)
        return res.returncode == 0
    except Exception:
        return False


def _mac_get_text() -> Optional[str]:
    try:
        res = _run(["pbpaste"])
        if res.returncode != 0:
            return None
        return res.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None


def _mac_clear() -> bool:
    # На macOS «очистки» как таковой нет — просто сбросим пустую строку
    return _mac_set_text("")


# -----------------------------
# Linux / BSD (Wayland/X11)
# -----------------------------


def _linux_set_text(text: str) -> bool:
    # Предпочитаем Wayland утилиты
    wl_copy = _which("wl-copy")
    if wl_copy:
        res = _run([wl_copy], input_text=text)
        if res.returncode == 0:
            return True
    # X11: xclip
    xclip = _which("xclip")
    if xclip:
        res = _run([xclip, "-selection", "clipboard"], input_text=text)
        if res.returncode == 0:
            return True
    # X11: xsel
    xsel = _which("xsel")
    if xsel:
        res = _run([xsel, "--clipboard", "--input"], input_text=text)
        if res.returncode == 0:
            return True
    # Фолбэк на pyperclip
    if _HAS_PYPERCLIP:
        try:
            pyperclip.copy(text)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
    return False


def _linux_get_text() -> Optional[str]:
    wl_paste = _which("wl-paste")
    if wl_paste:
        res = _run([wl_paste, "--no-newline"])
        if res.returncode == 0:
            return res.stdout.decode("utf-8", errors="replace")
    xclip = _which("xclip")
    if xclip:
        res = _run([xclip, "-selection", "clipboard", "-o"])
        if res.returncode == 0:
            return res.stdout.decode("utf-8", errors="replace")
    xsel = _which("xsel")
    if xsel:
        res = _run([xsel, "--clipboard", "--output"])
        if res.returncode == 0:
            return res.stdout.decode("utf-8", errors="replace")
    if _HAS_PYPERCLIP:
        try:
            return str(pyperclip.paste())  # type: ignore[attr-defined]
        except Exception:
            return None
    return None


def _linux_clear() -> bool:
    # В Wayland/X11 «очистка» — запись пустой строки
    return _linux_set_text("")


# -----------------------------
# Публичный API
# -----------------------------


def set_text(text: str) -> bool:
    """
    Пишет текст в системный буфер обмена.
    Возвращает True при успехе.
    """
    if _HAS_PYPERCLIP:
        try:
            pyperclip.copy(text)  # type: ignore[attr-defined]
            return True
        except Exception:
            # Падение — перейдем к системным способам
            pass

    if os.name == "nt":
        return _win_set_text(text)
    if sys.platform == "darwin":
        return _mac_set_text(text)
    return _linux_set_text(text)


def get_text() -> Optional[str]:
    """
    Читает текст из системного буфера обмена, если возможно.
    Возвращает строку или None.
    """
    if _HAS_PYPERCLIP:
        try:
            return str(pyperclip.paste())  # type: ignore[attr-defined]
        except Exception:
            pass

    if os.name == "nt":
        return _win_get_text()
    if sys.platform == "darwin":
        return _mac_get_text()
    return _linux_get_text()


def clear() -> bool:
    """
    Очищает буфер обмена (где возможно). На платформах без прямой очистки записывает пустую строку.
    """
    if os.name == "nt":
        return _win_clear()
    if sys.platform == "darwin":
        return _mac_clear()
    return _linux_clear()


def available_backend() -> str:
    """
    Возвращает строку-описание активного бэкенда.
    """
    if _HAS_PYPERCLIP:
        return "pyperclip"
    if os.name == "nt":
        return "winapi"
    if sys.platform == "darwin":
        return "pbcopy/pbpaste"
    wl = _which("wl-copy")
    if wl:
        return "wl-copy/wl-paste"
    if _which("xclip"):
        return "xclip"
    if _which("xsel"):
        return "xsel"
    return "unknown"


__all__ = [
    "set_text",
    "get_text",
    "clear",
    "available_backend",
]
