"""Windows side of LLM Explain: Win32 calls, Credential Manager, pynput triggers, pystray tray.
mac_support.py offers the same functions for macOS; llm_explain.py imports one of them as `plat`."""

import ctypes
import ctypes.wintypes as wt
import os
import time

import pyperclip
from pynput import keyboard, mouse

NAME = "Windows"
KEYSTORE = "Windows Credential Manager"
TRAY = "tray icon"
TRIGGER_CLICK = "Ctrl + Right-click"
NOTICE_CORNER = "br"  # startup notice near the tray: bottom-right
SUBPROCESS_FLAGS = {"creationflags": 0x08000000}  # CREATE_NO_WINDOW

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205


def setup_process():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI: coords match
    except Exception:
        user32.SetProcessDPIAware()


def setup_tk(root, icon_path):
    if os.path.exists(icon_path):
        root.iconbitmap(default=icon_path)  # icon for every popup window


def hotkey_label(hk):
    """'<ctrl>+<alt>+e' -> 'Ctrl+Alt+E'"""
    return "+".join(p.strip("<>").capitalize() if len(p.strip("<>")) > 1 else p.upper()
                    for p in (hk or "").split("+")) or "(no hotkey)"


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def work_area_at(x, y):
    """Usable screen rectangle (excluding taskbar) of the monitor containing (x, y)."""
    user32.MonitorFromPoint.restype = wt.HANDLE
    user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    hmon = user32.MonitorFromPoint(wt.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def single_instance_or_exit():
    kernel32.CreateMutexW(None, False, "Local\\LLMExplainSingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, "LLM Explain is already running (check the tray).",
                           "LLM Explain", 0x40)
        raise SystemExit(0)


# ------------------------------------------------------------ grab selection
_kb = keyboard.Controller()


def clipboard_text():
    try:
        return pyperclip.paste()
    except Exception:
        return ""


def copy_selection(restore=True):
    """Copy the current selection of the foreground program and return it."""
    # Wait for the user to let go of Alt/Shift/Win (from the hotkey), otherwise the
    # target program would see Ctrl+Alt+C instead of Ctrl+C.
    deadline = time.time() + 2.0
    while time.time() < deadline and any(key_down(k) for k in (VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)):
        time.sleep(0.02)

    old = clipboard_text()
    seq = user32.GetClipboardSequenceNumber()

    _kb.press(keyboard.Key.ctrl)
    _kb.press("c")
    _kb.release("c")
    _kb.release(keyboard.Key.ctrl)

    text = ""
    deadline = time.time() + 0.8
    while time.time() < deadline:
        time.sleep(0.03)
        if user32.GetClipboardSequenceNumber() != seq:
            time.sleep(0.05)  # let the owner finish writing
            text = clipboard_text()
            break

    if restore and text and old:
        try:
            pyperclip.copy(old)
        except Exception:
            pass
    return (text or "").strip()


# ---- API keys in Windows Credential Manager
class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
                ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]


_advapi32 = ctypes.windll.advapi32
_CRED_GENERIC, _CRED_PERSIST_LOCAL = 1, 2


def _cred_target(provider_name):
    return f"LLM Explain/{provider_name}"


def key_get(provider_name):
    p = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi32.CredReadW(_cred_target(provider_name), _CRED_GENERIC, 0, ctypes.byref(p)):
        return ""
    try:
        c = p.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    finally:
        _advapi32.CredFree(p)


def key_set(provider_name, secret):
    blob = secret.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = _CREDENTIAL()
    c.Type = _CRED_GENERIC
    c.TargetName = _cred_target(provider_name)
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = _CRED_PERSIST_LOCAL
    c.UserName = "LLM Explain"
    if not _advapi32.CredWriteW(ctypes.byref(c), 0):
        raise ctypes.WinError()


def key_delete(provider_name):
    _advapi32.CredDeleteW(_cred_target(provider_name), _CRED_GENERIC, 0)


# ---- look and feel
def system_dark_mode():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def style_titlebar(win, pal, dark):
    """Windows 11: dark/light title bar coloured like the window, rounded corners."""
    try:
        win.update_idletasks()
        hwnd = wt.HWND(int(win.wm_frame(), 16))
        dwm = ctypes.windll.dwmapi

        def set_attr(attr, val):
            v = ctypes.c_int(val)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def colorref(h):
            return int(h[1:3], 16) | int(h[3:5], 16) << 8 | int(h[5:7], 16) << 16

        set_attr(20, 1 if dark else 0)          # DWMWA_USE_IMMERSIVE_DARK_MODE
        set_attr(33, 2)                         # DWMWA_WINDOW_CORNER_PREFERENCE: round
        set_attr(34, colorref(pal["border"]))   # DWMWA_BORDER_COLOR
        set_attr(35, colorref(pal["bg"]))       # DWMWA_CAPTION_COLOR
        set_attr(36, colorref(pal["text"]))     # DWMWA_TEXT_COLOR
    except Exception:
        pass


def activate_app():
    pass  # Tk's focus_force is enough on Windows


def hide_app():
    pass


def show_app_quietly():
    pass


def open_file(path):
    os.startfile(path)


def lms_cli():
    path = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms.exe")
    return path if os.path.exists(path) else None


# ------------------------------------------------------------------ triggers
class Triggers:
    """Global hotkey (pynput) and Ctrl + Right-click (low-level mouse hook, click swallowed)."""

    def __init__(self, hotkey, ctrl_right_click, on_trigger):
        self.hotkey, self.ctrl_right_click, self.on_trigger = hotkey, ctrl_right_click, on_trigger
        self._rclick_swallowed = False
        self.mouse_listener = None

    def has_permission(self, prompt=False):
        return True

    def start(self):
        if self.hotkey:
            try:
                keyboard.GlobalHotKeys({self.hotkey: self.on_trigger}).start()
            except Exception as e:
                print(f"Could not register hotkey {self.hotkey!r}: {e}")
        if self.ctrl_right_click:
            def win32_filter(msg, data):
                # Runs inside the low-level mouse hook: must return quickly.
                if msg == WM_RBUTTONDOWN and key_down(VK_CONTROL) and not key_down(VK_MENU):
                    self._rclick_swallowed = True
                    self.on_trigger()
                    self.mouse_listener.suppress_event()
                elif msg == WM_RBUTTONUP and self._rclick_swallowed:
                    self._rclick_swallowed = False
                    self.mouse_listener.suppress_event()
                return True

            self.mouse_listener = mouse.Listener(win32_event_filter=win32_filter)
            self.mouse_listener.start()
        return True

    def stop(self):
        if self.mouse_listener:
            self.mouse_listener.stop()


# ---------------------------------------------------------------------- tray
class Tray:
    """items: [(label, callback or None for a disabled line, is_default)].
    Callbacks take no arguments (pystray passes none to a zero-argument callable)."""

    def __init__(self, icon_path, items):
        self.icon = None
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        try:
            img = Image.open(icon_path)
        except Exception:  # icon missing: draw a simple blue "?"
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((4, 4, 60, 60), fill=(26, 95, 180, 255))
            d.text((32, 32), "?", fill="white", anchor="mm")
        menu = pystray.Menu(*[
            pystray.MenuItem(label, cb, enabled=cb is not None, default=default)
            for label, cb, default in items])
        self.icon = pystray.Icon("llm_explain", img, "LLM Explain", menu)
        self.icon.run_detached()

    def stop(self):
        if self.icon:
            self.icon.stop()


SELFTEST_MODULES = ("anthropic", "pynput.keyboard", "pynput.mouse", "pystray", "PIL", "pyperclip")
