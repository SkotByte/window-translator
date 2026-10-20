"""System-wide hotkeys via Win32 RegisterHotKey (works while the game has focus, no keyboard hook)."""
import ctypes
import threading
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class HotkeyListener(QObject):
    triggered = Signal(str)      # hotkey name, delivered on the Qt main thread
    failed = Signal(str)         # hotkey name that could not be registered (already in use)

    def __init__(self, hotkeys: dict):
        super().__init__()
        self.hotkeys = hotkeys
        self._thread_id = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self):
        # RegisterHotKey binds to the calling thread, so register and pump messages here
        self._thread_id = kernel32.GetCurrentThreadId()
        names = {}
        for i, (name, (mods, key)) in enumerate(self.hotkeys.items(), start=1):
            flags = MOD_NOREPEAT
            for m in mods:
                flags |= MODS[m]
            if user32.RegisterHotKey(None, i, flags, ord(key.upper())):
                names[i] = name
            else:
                self.failed.emit(name)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in names:
                self.triggered.emit(names[msg.wParam])
        for i in names:
            user32.UnregisterHotKey(None, i)
