"""Win32 helpers for picking a target (game) window and tracking where it is on screen.

All rectangles here are in physical pixels (Qt makes the process per-monitor DPI aware).
"""
import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QRect

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2
SW_RESTORE = 9

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.MonitorFromWindow.restype = wintypes.HMONITOR


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    exe: str

    def label(self) -> str:
        return f"{self.title}  [{self.exe}]"


def _rect(r: wintypes.RECT) -> QRect:
    return QRect(r.left, r.top, r.right - r.left, r.bottom - r.top)


def _exe_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return "?"
    finally:
        kernel32.CloseHandle(handle)


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def list_windows() -> list[WindowInfo]:
    """Visible, titled top-level windows of other programs (what you see in Alt+Tab)."""
    own_pid = os.getpid()
    found: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        if user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = wintypes.DWORD()
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value or _window_pid(hwnd) == own_pid:
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        found.append(WindowInfo(hwnd, buf.value, _exe_name(hwnd)))
        return True

    user32.EnumWindows(callback, 0)
    return found


def is_valid(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def window_rect(hwnd: int) -> QRect:
    """Visible bounds of the window (without the invisible resize border)."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return _rect(r)


def monitor_of(hwnd: int) -> QRect:
    """Physical rect of the monitor showing (most of) the window."""
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(info))
    return _rect(info.rcMonitor)


def exclude_from_capture(hwnd: int) -> bool:
    """Keep a window out of screenshots (Windows 10 2004+), so it never gets OCR'd by mistake.

    Note: this also hides it from screen recording / streaming apps.
    """
    WDA_EXCLUDEFROMCAPTURE = 0x11
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


def bring_to_front(hwnd: int):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
