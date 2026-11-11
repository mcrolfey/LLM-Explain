"""macOS side of LLM Explain: Quartz event tap triggers, Cmd+C copy, Keychain, menu bar icon.
Same functions as win_support.py; llm_explain.py imports one of them as `plat`.

Tk on macOS runs the Cocoa main run loop, so the event tap and the menu bar item are attached
to that loop and their callbacks run on the main thread. They only put work on a queue.
"""

import fcntl
import logging
import os
import subprocess
import time

import AppKit
import Quartz
from Foundation import NSObject, NSUserDefaults

NAME = "macOS"
KEYSTORE = "the macOS Keychain"
TRAY = "menu bar icon"
TRIGGER_CLICK = "Control + right-click"
NOTICE_CORNER = "tr"  # startup notice near the menu bar icon: top-right
SUBPROCESS_FLAGS = {}

SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/LLM Explain")
LOG_PATH = os.path.expanduser("~/Library/Logs/LLM Explain.log")
LOG = logging.getLogger("llm_explain")

_M_SHIFT = Quartz.kCGEventFlagMaskShift
_M_CTRL = Quartz.kCGEventFlagMaskControl
_M_ALT = Quartz.kCGEventFlagMaskAlternate
_M_CMD = Quartz.kCGEventFlagMaskCommand
_M_ALL = _M_SHIFT | _M_CTRL | _M_ALT | _M_CMD

# Virtual key codes (US layout positions, kVK_ANSI_* / kVK_*)
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "`": 50, "space": 49, "tab": 48, "enter": 36, "return": 36, "esc": 53, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113,
    "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
}
_MODS = {"ctrl": _M_CTRL, "control": _M_CTRL, "alt": _M_ALT, "option": _M_ALT,
         "shift": _M_SHIFT, "cmd": _M_CMD, "command": _M_CMD, "super": _M_CMD}
_MOD_SYMBOLS = [(_M_CTRL, "⌃"), (_M_ALT, "⌥"), (_M_SHIFT, "⇧"), (_M_CMD, "⌘")]


def parse_hotkey(hk):
    """pynput-style '<ctrl>+<alt>+e' -> (modifier flags, key code), or None."""
    if not hk:
        return None
    mods, code = 0, None
    for part in hk.lower().split("+"):
        name = part.strip().strip("<>")
        name = name.split("_")[0] if name.startswith(("ctrl_", "alt_", "shift_", "cmd_")) else name
        if name in _MODS:
            mods |= _MODS[name]
        elif name in _KEYCODES:
            code = _KEYCODES[name]
        else:
            return None
    return (mods, code) if code is not None else None


def hotkey_label(hk):
    """'<ctrl>+<alt>+e' -> '⌃⌥E' (Mac style)."""
    parsed = parse_hotkey(hk)
    if not parsed:
        return "(no hotkey)"
    mods, code = parsed
    key = next(k for k, v in _KEYCODES.items() if v == code)
    return "".join(s for m, s in _MOD_SYMBOLS if mods & m) + (key.upper() if len(key) == 1 else key.capitalize())


# ------------------------------------------------------------------ process
def setup_process():
    pass


def setup_tk(root, icon_path):
    """Run as a menu bar app: no Dock icon, no app menu."""
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory)


_lock_file = None


def single_instance_or_exit():
    global _lock_file
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    _lock_file = open(os.path.join(SUPPORT_DIR, ".running.lock"), "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("LLM Explain is already running")
        alert.setInformativeText_("Look for its icon in the menu bar.")
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert.runModal()
        raise SystemExit(0)


def cursor_pos():
    """Mouse position in global top-left-origin points (the same coordinates Tk uses)."""
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return int(p.x), int(p.y)


def work_area_at(x, y):
    """Usable rectangle (without menu bar and Dock) of the screen containing (x, y),
    as (left, top, right, bottom) in top-left-origin points."""
    screens = AppKit.NSScreen.screens()
    if not screens:
        return 0, 0, 1440, 900
    primary_h = screens[0].frame().size.height

    def flip(r):
        left, top = r.origin.x, primary_h - (r.origin.y + r.size.height)
        return int(left), int(top), int(left + r.size.width), int(top + r.size.height)

    best, best_d = screens[0], None
    for s in screens:
        l, t, r, b = flip(s.frame())
        dx = max(l - x, 0, x - r)
        dy = max(t - y, 0, y - b)
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = s, d
    return flip(best.visibleFrame())


# ------------------------------------------------------------ grab selection
def _pb():
    return AppKit.NSPasteboard.generalPasteboard()


def clipboard_text():
    try:
        return _pb().stringForType_(AppKit.NSPasteboardTypeString) or ""
    except Exception:
        return ""


def _clipboard_snapshot():
    """Everything on the clipboard (all items, all types), so it can be put back."""
    snap = []
    try:
        for item in _pb().pasteboardItems() or []:
            data = {}
            for t in item.types():
                d = item.dataForType_(t)
                if d is not None:
                    data[t] = d
            if data:
                snap.append(data)
    except Exception:
        pass
    return snap


def _clipboard_restore(snap):
    pb = _pb()
    pb.clearContents()
    items = []
    for data in snap:
        item = AppKit.NSPasteboardItem.alloc().init()
        for t, d in data.items():
            item.setData_forType_(d, t)
        items.append(item)
    if items:
        pb.writeObjects_(items)


def _press_cmd_c():
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(src, _KEYCODES["c"], down)
        Quartz.CGEventSetFlags(e, _M_CMD)  # only Cmd, even if Control/Option are still held
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.01)


def copy_selection(restore=True):
    """Copy the frontmost app's selection with Cmd+C and return it."""
    # Let go of Option/Shift/Cmd from the hotkey first: some apps read the live key state.
    deadline = time.time() + 2.0
    while time.time() < deadline and Quartz.CGEventSourceFlagsState(
            Quartz.kCGEventSourceStateHIDSystemState) & (_M_ALT | _M_SHIFT | _M_CMD):
        time.sleep(0.02)

    old = _clipboard_snapshot() if restore else None
    seq = _pb().changeCount()
    _press_cmd_c()

    text = ""
    deadline = time.time() + 0.8
    while time.time() < deadline:
        time.sleep(0.03)
        if _pb().changeCount() != seq:
            time.sleep(0.05)  # let the app finish writing
            text = clipboard_text()
            break

    if restore and text and old:
        try:
            _clipboard_restore(old)
        except Exception:
            pass
    return (text or "").strip()


# ---- API keys in the Keychain (service "LLM Explain", account = provider name)
_SERVICE = "LLM Explain"
_keyring = None


def _kr():
    global _keyring
    if _keyring is None:
        from keyring.backends import macOS
        _keyring = macOS.Keyring()
    return _keyring


def key_get(provider_name):
    if not provider_name:
        return ""
    try:
        return _kr().get_password(_SERVICE, provider_name) or ""
    except Exception:
        return ""


def key_set(provider_name, secret):
    try:
        _kr().set_password(_SERVICE, provider_name, secret)
    except Exception as e:
        raise OSError(str(e)) from None


def key_delete(provider_name):
    try:
        _kr().delete_password(_SERVICE, provider_name)
    except Exception:
        pass


# ---- look and feel
def system_dark_mode():
    return NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle") == "Dark"


def style_titlebar(win, pal, dark):
    """Match the title bar to the theme, even when it differs from the system setting."""
    try:
        win.update_idletasks()
        win.tk.call("::tk::unsupported::MacWindowStyle", "appearance", win._w,
                    "darkaqua" if dark else "aqua")
    except Exception:
        pass


def _later(fn):
    """Run an AppKit call from the main run loop instead of inside a Tk callback.
    Calls like hide/unhide make Tk handle events synchronously, and Tk re-entering Python
    from inside a tkinter callback aborts the interpreter ("PyEval_RestoreThread")."""
    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


def activate_app():
    """Bring this app to the front so its window gets keyboard focus."""
    def go():
        app = AppKit.NSApplication.sharedApplication()
        app.unhide_(None)
        app.activateIgnoringOtherApps_(True)
    _later(go)


def hide_app():
    """Give focus back to the app the user was in (like closing Spotlight)."""
    _later(lambda: AppKit.NSApplication.sharedApplication().hide_(None))


def show_app_quietly():
    """Make windows visible again (after hide_app) without taking focus, e.g. for a toast."""
    _later(lambda: AppKit.NSApplication.sharedApplication().unhideWithoutActivation())


def open_file(path):
    subprocess.Popen(["open", "-t", path])


def open_accessibility_settings():
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])


def lms_cli():
    path = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms")
    return path if os.path.exists(path) else None


# ------------------------------------------------------------------ triggers
class Triggers:
    """Hotkey and Control + right-click through one Quartz event tap. The matching events are
    swallowed, so the hotkey isn't typed and the app under the mouse shows no context menu.
    Needs the Accessibility permission (System Settings → Privacy & Security)."""

    def __init__(self, hotkey, ctrl_right_click, on_trigger):
        self.hotkey = parse_hotkey(hotkey)
        self.ctrl_right_click = ctrl_right_click
        self.on_trigger = on_trigger
        self.tap = None
        self._rclick_swallowed = False
        self._key_swallowed = None

    def has_permission(self, prompt=False):
        try:
            import HIServices
            if prompt:
                return bool(HIServices.AXIsProcessTrustedWithOptions(
                    {HIServices.kAXTrustedCheckOptionPrompt: True}))
            return bool(HIServices.AXIsProcessTrusted())
        except Exception:
            return True  # can't tell: just try to create the tap

    def start(self):
        """Returns False if the tap couldn't be created (no permission yet); call again later."""
        if self.tap is not None:
            return True
        if not self.hotkey and not self.ctrl_right_click:
            return True
        types = [Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp]
        if self.ctrl_right_click:
            types += [Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp]
        mask = 0
        for t in types:
            mask |= Quartz.CGEventMaskBit(t)
        tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                      Quartz.kCGEventTapOptionDefault, mask, self._callback, None)
        if not tap:
            return False
        LOG.info("event tap created")
        self.tap = tap
        self._source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(), self._source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        return True

    def _callback(self, proxy, typ, event, refcon):
        # Runs on the main thread for every key press and right-click: keep it short.
        try:
            return self._handle(typ, event)
        except Exception:
            LOG.exception("event tap callback failed")
            return event

    def _handle(self, typ, event):
        if typ in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            LOG.warning("event tap was disabled by macOS (%s); re-enabling", typ)
            Quartz.CGEventTapEnable(self.tap, True)
            return event
        flags = Quartz.CGEventGetFlags(event)
        if typ == Quartz.kCGEventRightMouseDown:
            LOG.info("right-click seen, control=%s option=%s", bool(flags & _M_CTRL), bool(flags & _M_ALT))
            if self.ctrl_right_click and flags & _M_CTRL and not flags & _M_ALT:
                self._rclick_swallowed = True
                self.on_trigger()
                return None
        elif typ == Quartz.kCGEventRightMouseUp:
            if self._rclick_swallowed:
                self._rclick_swallowed = False
                return None
        elif self.hotkey:
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if typ == Quartz.kCGEventKeyDown and code == self.hotkey[1] \
                    and flags & _M_ALL == self.hotkey[0]:
                if not Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                    self.on_trigger()
                self._key_swallowed = code
                return None
            if typ == Quartz.kCGEventKeyUp and code == self._key_swallowed:
                self._key_swallowed = None
                return None
        return event

    def stop(self):
        if self.tap is not None:
            Quartz.CGEventTapEnable(self.tap, False)
            Quartz.CFRunLoopRemoveSource(Quartz.CFRunLoopGetMain(), self._source,
                                         Quartz.kCFRunLoopCommonModes)
            self.tap = None


# ---------------------------------------------------------------------- tray
class _MenuTarget(NSObject):
    def initWithCallbacks_(self, callbacks):
        self = self.init()
        if self is not None:
            self.callbacks = callbacks
        return self

    def clicked_(self, sender):
        cb = self.callbacks.get(sender.tag())
        if cb:
            cb()


class Tray:
    """Menu bar icon. items: [(label, callback or None for a disabled line, is_default)]."""

    def __init__(self, icon_path, items):
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        button = self.item.button()
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path) if os.path.exists(icon_path) else None
        if img is not None:
            img.setSize_((18, 18))
            img.setTemplate_(True)  # drawn in the menu bar's colour (light/dark)
            button.setImage_(img)
        else:
            button.setTitle_("?⃝")
        button.setToolTip_("LLM Explain")

        callbacks = {}
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.target = _MenuTarget.alloc().initWithCallbacks_(callbacks)
        for i, (label, cb, _default) in enumerate(items):
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, "clicked:", "")
            mi.setTag_(i)
            mi.setTarget_(self.target)
            mi.setEnabled_(cb is not None)
            if label == "Quit":
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(mi)
            if cb:
                callbacks[i] = cb
        self.menu = menu
        self.item.setMenu_(menu)

    def stop(self):
        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)


SELFTEST_MODULES = ("anthropic", "AppKit", "Quartz", "HIServices", "keyring.backends.macOS", "PIL")
