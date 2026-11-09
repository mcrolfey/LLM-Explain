"""
LLM Explain: highlight text in any Windows program, then press Ctrl + Right-click
(or the hotkey, default Ctrl+Alt+E) to get an explanation from a local LM Studio model.

How it works
  1. A low-level mouse hook watches for Ctrl + Right-click and swallows that click,
     so the program under the mouse does not open its own context menu.
  2. The tool sends Ctrl+C to the foreground program to copy the highlighted text,
     reads it from the clipboard, then puts your previous clipboard text back.
  3. The text goes to LM Studio's OpenAI-compatible server (/v1/chat/completions)
     and the answer streams into a small popup near the mouse. You can ask follow-ups.

Run with pythonw.exe (no console) via start_llm_explain.bat. Quit from the tray icon.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import font as tkfont

import pyperclip
from pynput import keyboard, mouse

FROZEN = getattr(sys, "frozen", False)  # running as a PyInstaller exe
# config.json lives next to the .exe (or next to this script when run with Python)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
# bundled read-only files (icon) live in the PyInstaller temp dir when frozen
RES = getattr(sys, "_MEIPASS", HERE)
CONFIG_PATH = os.path.join(HERE, "config.json")
ICON_ICO = os.path.join(RES, "icon.ico")

DEFAULT_CONFIG = {
    "base_url": "http://localhost:1234",
    "model": "",
    "fallback_model": "",
    "hotkey": "<ctrl>+<alt>+e",
    "ctrl_right_click": True,
    "restore_clipboard": True,
    "temperature": 0.3,
    "max_tokens": 1200,
    "font_size": 11,
    "theme": "auto",
    "length": "standard",
    "auto_unload_minutes": 0,
    "system_prompt": (
        "You are a concise explainer. The user highlighted some text in another program and "
        "wants to understand it. First give a one-line plain-language meaning. Then explain it "
        "clearly in a short paragraph or a few bullet points: definition, context, and an example "
        "if useful. If it is code, explain what it does. If it is in a foreign language, translate "
        "it and explain it. If it is a technical or academic term, give the field it belongs to. "
        "Do not repeat the text back in full. Use plain text, not markdown tables."
    ),
}

CONFIG_ERROR = None


def hotkey_label(hk):
    """'<ctrl>+<alt>+e' -> 'Ctrl+Alt+E'"""
    return "+".join(p.strip("<>").capitalize() if len(p.strip("<>")) > 1 else p.upper()
                    for p in (hk or "").split("+")) or "(no hotkey)"


def load_config():
    global CONFIG_ERROR
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        try:  # first run: write a config the user can edit
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        except OSError:
            pass
    except json.JSONDecodeError as e:
        CONFIG_ERROR = f"config.json has an error and was ignored (defaults used):\n{e}"
    # "localhost" makes Windows try IPv6 (::1) first; LM Studio listens on IPv4, so every
    # request would wait ~2 s for the fallback. Go straight to 127.0.0.1.
    cfg["base_url"] = cfg["base_url"].rstrip("/").replace("://localhost", "://127.0.0.1")
    return cfg


CFG = load_config()

# ---------------------------------------------------------------- Win32 helpers
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205


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
        sys.exit(0)


# ------------------------------------------------------------ grab selection
_kb = keyboard.Controller()


def copy_selection():
    """Copy the current selection of the foreground program and return it."""
    # Wait for the user to let go of Alt/Shift/Win (from the hotkey), otherwise the
    # target program would see Ctrl+Alt+C instead of Ctrl+C.
    deadline = time.time() + 2.0
    while time.time() < deadline and any(key_down(k) for k in (VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)):
        time.sleep(0.02)

    try:
        old = pyperclip.paste()
    except Exception:
        old = ""
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
            try:
                text = pyperclip.paste()
            except Exception:
                text = ""
            break

    if CFG.get("restore_clipboard", True) and text and old:
        try:
            pyperclip.copy(old)
        except Exception:
            pass
    return (text or "").strip()


# ------------------------------------------------------------- LM Studio API
def _get_json(path, timeout=5):
    with urllib.request.urlopen(CFG["base_url"] + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_model():
    """Configured model > a model already loaded in LM Studio > fallback > first chat model."""
    if CFG.get("model"):
        return CFG["model"]
    try:
        models = _get_json("/api/v0/models").get("data", [])
        chat = [m for m in models if m.get("type") in ("llm", "vlm")]
        loaded = [m for m in chat if m.get("state") == "loaded"]
        if loaded:
            return loaded[0]["id"]
        if CFG.get("fallback_model"):
            return CFG["fallback_model"]
        if chat:
            return chat[0]["id"]
    except Exception:
        pass
    if CFG.get("fallback_model"):
        return CFG["fallback_model"]
    models = _get_json("/v1/models").get("data", [])
    ids = [m["id"] for m in models if "embed" not in m["id"].lower()]
    if not ids:
        raise RuntimeError("No chat model found in LM Studio.")
    return ids[0]


def stream_chat(messages, model, cancel, max_tokens=None):
    """Yield content chunks from LM Studio's streaming chat endpoint."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": CFG["temperature"],
        "max_tokens": max_tokens or CFG["max_tokens"],
        "stream": True,
    }
    if CFG.get("auto_unload_minutes"):
        # LM Studio unloads a model it auto-loaded after this many idle seconds
        payload["ttl"] = int(float(CFG["auto_unload_minutes"]) * 60)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(CFG["base_url"] + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    # Generous timeout: the first request may have to load the model into memory.
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            if cancel.is_set():
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if "error" in obj:
                raise RuntimeError(str(obj["error"]))
            choices = obj.get("choices") or [{}]
            delta = choices[0].get("delta", {})
            if delta.get("reasoning_content") or delta.get("reasoning"):
                yield ("thinking", "")
            if delta.get("content"):
                yield ("content", delta["content"])


def loaded_models():
    """[(display name, instance id)] of models currently loaded in LM Studio."""
    data = _get_json("/api/v1/models")
    return [(m.get("display_name") or m.get("key"), inst["id"])
            for m in data.get("models", []) for inst in m.get("loaded_instances", [])]


def _lms_cli():
    path = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms.exe")
    return path if os.path.exists(path) else None


def unload_models():
    """Unload every model loaded in LM Studio. Returns a short message for the user."""
    try:
        loaded = loaded_models()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # Older LM Studio without the /api/v1 REST API: use its command-line tool
        lms = _lms_cli()
        if not lms:
            return "This LM Studio version can't unload remotely. Update LM Studio."
        import subprocess
        subprocess.run([lms, "unload", "--all"], capture_output=True, timeout=60,
                       creationflags=0x08000000)  # CREATE_NO_WINDOW
        return "Unloaded all models."
    if not loaded:
        return "No model is loaded in LM Studio."
    for _, iid in loaded:
        req = urllib.request.Request(CFG["base_url"] + "/api/v1/models/unload",
                                     data=json.dumps({"instance_id": iid}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    names = ", ".join(sorted({n for n, _ in loaded}))
    return f"Unloaded {names} · reloads when needed"


def unload_models_safe():
    try:
        return unload_models()
    except urllib.error.URLError as e:
        return f"Couldn't reach LM Studio to unload ({getattr(e, 'reason', e)})."
    except Exception as e:
        return f"Unload failed: {type(e).__name__}: {e}"


THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def visible_text(raw):
    """Hide <think>...</think> blocks, including an unfinished one at the end."""
    out = THINK_RE.sub("", raw)
    i = out.find("<think>")
    if i != -1:
        out = out[:i]
    for n in range(len("<think>") - 1, 0, -1):  # partial tag still arriving
        if out.endswith("<think>"[:n]):
            out = out[:-n]
            break
    return out.lstrip()


# ------------------------------------------------------------------- popup UI
# Answer length: (label, extra system instruction, max_tokens override)
LENGTHS = {
    "brief": ("Brief",
              "LENGTH: Override any length guidance above. Reply with only the plain meaning "
              "in 1-2 short sentences. No lists, no headings, no examples.", 400),
    "standard": ("Standard", "", None),
    "detailed": ("Detailed",
                 "LENGTH: Give a thorough explanation: background, how it works, a concrete "
                 "example, and related concepts or common confusions. Use short headings or "
                 "bullet points where they help.", 2500),
}
LENGTH_ORDER = ["brief", "standard", "detailed"]

THEMES = {
    "light": dict(bg="#ffffff", subtle="#f2f3f5", hover="#e6e8ec", border="#e1e3e7",
                  text="#1f2328", muted="#6b7280", accent="#2563eb", accent_hover="#1d4ed8",
                  accent_fg="#ffffff", accent_soft="#dbe7fd", code_bg="#f0f2f4", error="#c62828"),
    "dark": dict(bg="#1f2023", subtle="#2b2d31", hover="#373a40", border="#3a3d43",
                 text="#e8e8ea", muted="#9aa0a8", accent="#7aa7ff", accent_hover="#94b8ff",
                 accent_fg="#0f1420", accent_soft="#2c3c5e", code_bg="#2b2d31", error="#ff8a80"),
}


def system_dark_mode():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def current_theme():
    """Returns (palette, is_dark). config "theme": "auto" | "light" | "dark"."""
    name = CFG.get("theme", "auto")
    if name not in THEMES:
        name = "dark" if system_dark_mode() else "light"
    return THEMES[name], name == "dark"


def save_setting(key, value):
    """Remember a setting in config.json without touching the other keys."""
    CFG[key] = value
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except Exception:
        return  # broken config: don't overwrite the user's file
    data[key] = value
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


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


_FONT_FAMILIES = None


def pick_font(root, *names):
    global _FONT_FAMILIES
    if _FONT_FAMILIES is None:
        _FONT_FAMILIES = set(tkfont.families(root))
    return next((n for n in names if n in _FONT_FAMILIES), names[-1])


class FlatButton(tk.Label):
    """Borderless button with hover colour. kind: "ghost" or "accent"."""

    def __init__(self, master, text, command, pal, font, kind="ghost", padx=10, pady=4):
        super().__init__(master, text=text, font=font, padx=padx, pady=pady, cursor="hand2")
        self.pal, self.kind, self.command, self.enabled = pal, kind, command, True
        self._paint()
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def _paint(self, hover=False):
        p = self.pal
        if self.kind == "accent":
            if self.enabled:
                self.config(bg=p["accent_hover"] if hover else p["accent"], fg=p["accent_fg"])
            else:
                self.config(bg=p["subtle"], fg=p["muted"])
        else:
            self.config(bg=p["hover"] if (hover and self.enabled) else self.master["bg"],
                        fg=p["text"] if self.enabled else p["muted"])

    def set_enabled(self, on):
        self.enabled = on
        self.config(cursor="hand2" if on else "arrow")
        self._paint()


class Segmented(tk.Frame):
    """Pill-style segmented control."""

    def __init__(self, master, options, value, command, pal, font, font_sel, px):
        super().__init__(master, bg=pal["subtle"], padx=px(3), pady=px(3))
        self.pal, self.font, self.font_sel = pal, font, font_sel
        self.labels = {}
        for key, label in options:
            l = tk.Label(self, text=label, padx=px(10), pady=px(2), cursor="hand2")
            l.pack(side="left")
            l.bind("<Button-1>", lambda e, k=key: command(k))
            l.bind("<Enter>", lambda e, k=key: self._hover(k, True))
            l.bind("<Leave>", lambda e, k=key: self._hover(k, False))
            self.labels[key] = l
        self.set(value)

    def set(self, value):
        self.value = value
        for k, l in self.labels.items():
            sel = k == value
            l.config(bg=self.pal["bg"] if sel else self.pal["subtle"],
                     fg=self.pal["accent"] if sel else self.pal["muted"],
                     font=self.font_sel if sel else self.font)

    def _hover(self, key, on):
        if key != self.value:
            self.labels[key].config(fg=self.pal["text"] if on else self.pal["muted"])


class ExplainWindow:
    def __init__(self, root, selected, x, y):
        self.root = root
        self.selected = selected
        self.closed = False
        self.q = queue.Queue()
        self.busy = False
        self.rid = 0                         # id of the current request
        self.req_cancel = threading.Event()  # cancels the current request
        self.tick = 0
        self.length = CFG.get("length", "standard")
        if self.length not in LENGTHS:
            self.length = "standard"
        # the system prompt is added per request, so the length can change
        self.messages = [{"role": "user",
                          "content": f"Explain this text:\n\n\"\"\"\n{selected}\n\"\"\""}]
        self.segments = []   # finished (kind, text) for display
        self.raw = ""        # answer being streamed, raw
        self.model = None

        p, dark = current_theme()
        self.pal = p
        w = self.win = tk.Toplevel(root, bg=p["bg"])
        w.withdraw()  # build hidden, show once positioned
        w.title("LLM Explain")
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.bind("<Escape>", lambda e: self.close())
        for i, k in enumerate(LENGTH_ORDER, 1):
            w.bind(f"<Control-Key-{i}>", lambda e, k=k: self.set_length(k))

        scale = w.winfo_fpixels("1i") / 96.0
        px = self.px = lambda v: max(1, int(round(v * scale)))

        fs = CFG["font_size"]
        ui = pick_font(root, "Segoe UI Variable Text", "Segoe UI", "TkDefaultFont")
        ui_disp = pick_font(root, "Segoe UI Variable Display", "Segoe UI", "TkDefaultFont")
        mono = pick_font(root, "Cascadia Mono", "Consolas", "Courier New")
        self.f_body = tkfont.Font(family=ui, size=fs)
        self.f_bold = tkfont.Font(family=ui, size=fs, weight="bold")
        self.f_head = tkfont.Font(family=ui_disp, size=fs + 2, weight="bold")
        self.f_title = tkfont.Font(family=ui_disp, size=fs + 1, weight="bold")
        self.f_code = tkfont.Font(family=mono, size=fs - 1)
        self.f_small = tkfont.Font(family=ui, size=fs - 2)
        self.f_small_b = tkfont.Font(family=ui, size=fs - 2, weight="bold")
        self.f_quote = tkfont.Font(family=ui, size=fs - 1, slant="italic")
        self.f_italic = tkfont.Font(family=ui, size=fs, slant="italic")

        # ---- header: title + length switch
        header = tk.Frame(w, bg=p["bg"])
        header.pack(fill="x", padx=px(18), pady=(px(14), px(10)))
        tk.Label(header, text="Explain", font=self.f_title, bg=p["bg"], fg=p["text"]).pack(side="left")
        self.seg = Segmented(header, [(k, LENGTHS[k][0]) for k in LENGTH_ORDER], self.length,
                             self.set_length, p, self.f_small, self.f_small_b, px)
        self.seg.pack(side="right")

        # ---- quote chip with the highlighted text
        preview = " ".join(selected.split())
        if len(preview) > 220:
            preview = preview[:220] + "…"
        quote = tk.Frame(w, bg=p["subtle"])
        quote.pack(fill="x", padx=px(18), pady=(0, px(8)))
        tk.Frame(quote, bg=p["accent"], width=px(3)).pack(side="left", fill="y")
        tk.Label(quote, text=preview, font=self.f_quote, bg=p["subtle"], fg=p["muted"],
                 wraplength=px(500), justify="left", anchor="w",
                 padx=px(10), pady=px(7)).pack(side="left", fill="x", expand=True)

        # ---- answer area (scroll with the mouse wheel)
        t = self.text = tk.Text(
            w, wrap="word", font=self.f_body, relief="flat", bd=0, highlightthickness=0,
            bg=p["bg"], fg=p["text"], padx=px(18), pady=px(6), width=58, height=13,
            cursor="arrow", insertwidth=0, selectbackground=p["accent_soft"],
            selectforeground=p["text"], inactiveselectbackground=p["accent_soft"],
            spacing1=px(1), spacing2=px(3), spacing3=px(3))
        t.pack(fill="both", expand=True)
        t.tag_configure("bold", font=self.f_bold)
        t.tag_configure("head", font=self.f_head, spacing1=px(10), spacing3=px(4))
        t.tag_configure("code", font=self.f_code, background=p["code_bg"])
        t.tag_configure("codeblock", font=self.f_code, background=p["code_bg"],
                        lmargin1=px(10), lmargin2=px(10), rmargin=px(10))
        t.tag_configure("li0", lmargin1=px(4), lmargin2=px(22))
        t.tag_configure("li1", lmargin1=px(22), lmargin2=px(40))
        t.tag_configure("mark", foreground=p["accent"], font=self.f_bold)
        t.tag_configure("youlabel", font=self.f_small_b, foreground=p["accent"], spacing1=px(16))
        t.tag_configure("you", font=self.f_bold, spacing3=px(8))
        t.tag_configure("muted", font=self.f_italic, foreground=p["muted"])
        t.tag_configure("error", foreground=p["error"])
        # read-only, but text can still be selected and copied
        t.bind("<Key>", lambda e: None if (e.state & 0x4 and e.keysym.lower() in ("c", "a")) else "break")

        # ---- footer: follow-up input
        tk.Frame(w, bg=p["border"], height=1).pack(fill="x")
        foot = tk.Frame(w, bg=p["bg"])
        foot.pack(fill="x", padx=px(16), pady=(px(12), px(4)))
        self.box = tk.Frame(foot, bg=p["subtle"], highlightthickness=1,
                            highlightbackground=p["border"], highlightcolor=p["border"])
        self.box.pack(side="left", fill="x", expand=True)
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(self.box, textvariable=self.entry_var, font=self.f_body,
                              relief="flat", bd=0, highlightthickness=0, bg=p["subtle"],
                              fg=p["text"], insertbackground=p["text"],
                              selectbackground=p["accent_soft"], selectforeground=p["text"])
        self.entry.pack(side="left", fill="x", expand=True, padx=px(10), pady=px(7))
        self.entry.bind("<Return>", lambda e: self.follow_up())
        self.entry.bind("<FocusIn>", lambda e: self.box.config(highlightbackground=p["accent"],
                                                               highlightcolor=p["accent"]))
        self.entry.bind("<FocusOut>", lambda e: self.box.config(highlightbackground=p["border"],
                                                                highlightcolor=p["border"]))
        self.placeholder = tk.Label(self.box, text="Ask a follow-up…", font=self.f_body,
                                    bg=p["subtle"], fg=p["muted"], cursor="xterm")
        self.placeholder.place(in_=self.entry, x=0, rely=0.5, anchor="w")
        self.placeholder.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry_var.trace_add("write", lambda *a: self._placeholder())
        self.send_btn = FlatButton(foot, "Ask", self.follow_up, p, self.f_bold, kind="accent",
                                   padx=px(16), pady=px(6))
        self.send_btn.pack(side="left", padx=(px(8), 0), fill="y")

        # ---- status row
        srow = tk.Frame(w, bg=p["bg"])
        srow.pack(fill="x", padx=px(14), pady=(px(4), px(10)))
        # width=1: long status text must not stretch the window (it is clipped instead)
        self.status = tk.Label(srow, text="", font=self.f_small, bg=p["bg"], fg=p["muted"],
                               anchor="w", width=1)
        self.status.pack(side="left", fill="x", expand=True, padx=(px(4), 0))
        self.copy_btn = FlatButton(srow, "Copy", self.copy_answer, p, self.f_small,
                                   padx=px(9), pady=px(3))
        self.copy_btn.pack(side="right")
        self.regen_btn = FlatButton(srow, "Regenerate", self.regenerate, p, self.f_small,
                                    padx=px(9), pady=px(3))
        self.regen_btn.pack(side="right", padx=(0, px(4)))
        self.unload_btn = FlatButton(srow, "Unload model", self.unload, p, self.f_small,
                                     padx=px(9), pady=px(3))
        self.unload_btn.pack(side="right", padx=(0, px(4)))

        w.minsize(px(400), px(280))
        self.place(x, y)
        # Windows applies the launcher's "start minimized/hidden" flag to the first window
        # a process shows; force normal state so the popup is never born minimized.
        w.deiconify()
        w.after(30, lambda: w.state("normal") if w.state() == "iconic" else None)
        style_titlebar(w, p, dark)
        w.lift()
        w.focus_force()
        self.entry.focus_set()

        self.start_request()
        self.poll()

    def place(self, x, y):
        self.win.update_idletasks()
        ww, wh = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        left, top, right, bottom = work_area_at(x, y)
        nx = min(max(left, x + 16), right - ww - 8)
        ny = min(max(top, y + 16), bottom - wh - 40)
        self.win.geometry(f"+{nx}+{max(top, ny)}")

    def _placeholder(self):
        if self.entry_var.get():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(in_=self.entry, x=0, rely=0.5, anchor="w")

    # ---- requests
    def start_request(self):
        self.req_cancel.set()                # stop any answer still streaming
        self.req_cancel = threading.Event()
        self.rid += 1
        self.busy = True
        self.raw = ""
        self.send_btn.set_enabled(False)
        self.regen_btn.set_enabled(False)
        self.status.config(text="Contacting LM Studio…")
        label, instr, max_tokens = LENGTHS[self.length]
        system = CFG["system_prompt"] + ("\n\n" + instr if instr else "")
        msgs = [{"role": "system", "content": system}] + list(self.messages)
        max_tokens = max(max_tokens, CFG["max_tokens"]) if self.length == "detailed" else \
            (max_tokens or CFG["max_tokens"])
        threading.Thread(target=self._worker, args=(self.rid, self.req_cancel, msgs, max_tokens),
                         daemon=True).start()
        self.render()

    def _worker(self, rid, cancel, msgs, max_tokens):
        put = lambda kind, val=None: self.q.put((rid, kind, val))
        try:
            if self.model is None:
                self.model = resolve_model()
            put("status", f"{self.model} · waiting for the model…")
            for kind, chunk in stream_chat(msgs, self.model, cancel, max_tokens):
                put(kind, chunk)
            if not cancel.is_set():
                put("done")
        except urllib.error.URLError as e:
            put("error", "Could not reach LM Studio at "
                f"{CFG['base_url']}.\nIn LM Studio open the Developer tab and start the "
                f"server, then press Regenerate.\n\n({e.reason})")
        except Exception as e:
            put("error", f"{type(e).__name__}: {e}")

    def _idle(self, text):
        self.busy = False
        self.send_btn.set_enabled(True)
        self.regen_btn.set_enabled(True)
        self.status.config(text=text)

    def poll(self):
        if self.closed:
            return
        changed = False
        try:
            while True:
                rid, kind, val = self.q.get_nowait()
                if rid != self.rid:
                    continue  # from a cancelled request
                if kind == "content":
                    self.raw += val
                    changed = True
                    self.status.config(text=f"{self.model} · writing…")
                elif kind == "thinking":
                    self.status.config(text=f"{self.model} · thinking…")
                elif kind == "status":
                    self.status.config(text=val)
                elif kind == "unloaded":
                    self.unload_btn.set_enabled(True)
                    self.status.config(text=val.split(" · ")[0])  # short form fits the row
                elif kind == "done":
                    answer = visible_text(self.raw).strip()
                    self.messages.append({"role": "assistant", "content": answer})
                    self.segments.append(("answer", answer))
                    self.raw = ""
                    self._idle(f"{self.model} · {LENGTHS[self.length][0].lower()} · Esc to close")
                    changed = True
                elif kind == "error":
                    self.segments.append(("error", val))
                    self.raw = ""
                    self._idle("Error")
                    changed = True
        except queue.Empty:
            pass
        self.tick += 1
        if self.busy and not visible_text(self.raw) and self.tick % 8 == 0:
            changed = True  # animate "Thinking…"
        if changed:
            self.render()
        self.win.after(50, self.poll)

    def follow_up(self):
        q = self.entry_var.get().strip()
        if not q or self.busy:
            return
        self.entry_var.set("")
        if self.messages[-1]["role"] == "user" and len(self.messages) > 1:
            self.messages.pop()  # the previous follow-up failed; replace it
        self.messages.append({"role": "user", "content": q})
        self.segments.append(("you", q))
        self.start_request()

    def unload(self):
        """Free the model's memory in LM Studio (stops an answer that's still streaming)."""
        if self.busy:
            self.req_cancel.set()
            self.rid += 1  # ignore whatever the stopped request still sends
            if visible_text(self.raw).strip():
                self.segments.append(("answer", visible_text(self.raw).strip() + " …"))
            self.raw = ""
            self._idle("")
            self.render()
        self.unload_btn.set_enabled(False)
        self.status.config(text="Unloading model…")

        def work():
            msg = unload_models_safe()
            self.q.put((self.rid, "unloaded", msg))
        threading.Thread(target=work, daemon=True).start()

    def regenerate(self):
        """Drop the last answer (or error) and ask again, e.g. at a new length."""
        if self.messages[-1]["role"] == "assistant":
            self.messages.pop()
        while self.segments and self.segments[-1][0] in ("answer", "error"):
            self.segments.pop()
        self.start_request()

    def set_length(self, key):
        if key == self.length:
            return
        self.length = key
        self.seg.set(key)
        save_setting("length", key)
        self.regenerate()

    # ---- display
    def render(self):
        t = self.text
        at_bottom = t.yview()[1] > 0.98
        t.delete("1.0", "end")
        for kind, txt in self.segments:
            if kind == "you":
                t.insert("end", "YOU\n", "youlabel")
                t.insert("end", txt + "\n", "you")
            elif kind == "error":
                t.insert("end", txt + "\n", "error")
            else:
                self.insert_markdown(txt)
        if self.busy:
            shown = visible_text(self.raw)
            if shown:
                self.insert_markdown(shown)
            else:
                t.insert("end", "Thinking" + "." * (1 + (self.tick // 8) % 3), "muted")
        if at_bottom or self.busy:
            t.see("end")

    def insert_markdown(self, md):
        """Small markdown renderer: headings, bullets, numbered lists, **bold**, `code`, ``` blocks."""
        t = self.text
        in_code = False
        for line in md.strip("\n").split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                t.insert("end", line + "\n", "codeblock")
                continue
            m = re.match(r"^\s{0,3}#{1,6}\s+(.*)", line)
            if m:
                t.insert("end", m.group(1).replace("**", "") + "\n", "head")
                continue
            line_tag = ()
            m = re.match(r"^(\s*)([*\-+]|\d+[.)])\s+(.*)", line)
            if m:
                line_tag = ("li1",) if len(m.group(1)) >= 2 else ("li0",)
                mark = "•" if m.group(2) in "*-+" else m.group(2)
                t.insert("end", mark + "  ", ("mark",) + line_tag)
                line = m.group(3)
            m = re.match(r"^([A-Z][\w ()/&-]{1,28}):(\s|$)", line)
            if m:  # "Meaning: ..." -> bold label
                t.insert("end", m.group(1) + ":", ("bold",) + line_tag)
                line = line[len(m.group(1)) + 1:]
            for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", line):
                if part.startswith("**") and part.endswith("**") and len(part) > 4:
                    t.insert("end", part[2:-2], ("bold",) + line_tag)
                elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                    t.insert("end", part[1:-1], ("code",) + line_tag)
                elif part:
                    t.insert("end", part, line_tag)
            t.insert("end", "\n", line_tag)

    def copy_answer(self):
        answers = [txt for kind, txt in self.segments if kind == "answer"]
        if answers:
            self.root.clipboard_clear()
            self.root.clipboard_append(answers[-1])
            self.status.config(text="Copied to clipboard ✓")

    def close(self):
        self.closed = True
        self.req_cancel.set()
        self.win.destroy()


# ------------------------------------------------------------------- triggers
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        if os.path.exists(ICON_ICO):
            self.root.iconbitmap(default=ICON_ICO)  # icon for every popup window
        self.ui_q = queue.Queue()
        self.trigger_q = queue.Queue()
        self._rclick_swallowed = False
        self.mouse_listener = None
        self.tray = None

        threading.Thread(target=self._trigger_worker, daemon=True).start()
        self._start_hotkey()
        if CFG.get("ctrl_right_click", True):
            self._start_mouse_hook()
        self._start_tray()
        self.root.after(50, self._poll)
        self.root.after(300, self._startup_notice)

    def _startup_notice(self):
        """The app lives in the tray, so show briefly that launching worked."""
        left, top, right, bottom = work_area_at(*cursor_pos())
        msg = (f"LLM Explain is running.\nHighlight text, then Ctrl + Right-click "
               f"(or {hotkey_label(CFG.get('hotkey'))}).\nQuit from the tray icon.")
        if CONFIG_ERROR:
            msg += "\n\n" + CONFIG_ERROR
        self._toast(msg, right - 20, bottom - 20, ms=6000 if CONFIG_ERROR else 3500, anchor_br=True)

    def _trigger_worker(self):
        while True:
            source = self.trigger_q.get()
            # drop duplicate triggers that piled up
            while not self.trigger_q.empty():
                self.trigger_q.get_nowait()
            x, y = cursor_pos()
            if source == "clipboard":
                try:
                    text = pyperclip.paste().strip()
                except Exception:
                    text = ""
            else:
                time.sleep(0.05)
                text = copy_selection()
            self.ui_q.put(("open", text, x, y))

    def _start_hotkey(self):
        hk = CFG.get("hotkey")
        if not hk:
            return
        try:
            keyboard.GlobalHotKeys({hk: lambda: self.trigger_q.put("selection")}).start()
        except Exception as e:
            print(f"Could not register hotkey {hk!r}: {e}")

    def _start_mouse_hook(self):
        def win32_filter(msg, data):
            # Runs inside the low-level mouse hook: must return quickly.
            if msg == WM_RBUTTONDOWN and key_down(VK_CONTROL) and not key_down(VK_MENU):
                self._rclick_swallowed = True
                self.trigger_q.put("selection")
                self.mouse_listener.suppress_event()
            elif msg == WM_RBUTTONUP and self._rclick_swallowed:
                self._rclick_swallowed = False
                self.mouse_listener.suppress_event()
            return True

        self.mouse_listener = mouse.Listener(win32_event_filter=win32_filter)
        self.mouse_listener.start()

    def _tray_unload(self):
        threading.Thread(target=lambda: self.ui_q.put(("toast", unload_models_safe())),
                         daemon=True).start()

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        try:
            img = Image.open(ICON_ICO)
        except Exception:  # icon missing: draw a simple blue "?"
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((4, 4, 60, 60), fill=(26, 95, 180, 255))
            d.text((32, 32), "?", fill="white", anchor="mm")
        menu = pystray.Menu(
            pystray.MenuItem("Explain clipboard text", lambda: self.trigger_q.put("clipboard")),
            pystray.MenuItem(f"Trigger: Ctrl+Right-click or {hotkey_label(CFG.get('hotkey'))}", None, enabled=False),
            pystray.MenuItem("Unload model from LM Studio", self._tray_unload),
            pystray.MenuItem("Open config.json", lambda: os.startfile(CONFIG_PATH)),
            pystray.MenuItem("Quit", lambda: self.ui_q.put(("quit",))),
        )
        self.tray = pystray.Icon("llm_explain", img, "LLM Explain", menu)
        self.tray.run_detached()

    def _poll(self):
        try:
            while True:
                item = self.ui_q.get_nowait()
                if item[0] == "quit":
                    self.quit()
                    return
                if item[0] == "toast":
                    left, top, right, bottom = work_area_at(*cursor_pos())
                    self._toast(item[1], right - 20, bottom - 20, ms=3500, anchor_br=True)
                    continue
                _, text, x, y = item
                if not text:
                    self._toast("No text was selected (or the program blocked copying).", x, y)
                else:
                    ExplainWindow(self.root, text, x, y)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _toast(self, msg, x, y, ms=1800, anchor_br=False):
        tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        pal, _ = current_theme()
        tw.config(bg=pal["border"])
        tk.Label(tw, text=msg, bg=pal["subtle"], fg=pal["text"], padx=14, pady=10,
                 justify="left").pack(padx=1, pady=1)
        if anchor_br:  # (x, y) is the bottom-right corner
            tw.update_idletasks()
            x, y = x - tw.winfo_reqwidth() - 12, y - tw.winfo_reqheight() - 12
        tw.geometry(f"+{x + 12}+{y + 12}")
        tw.deiconify()
        tw.bind("<Button-1>", lambda e: tw.destroy())
        tw.after(ms, tw.destroy)

    def quit(self):
        if self.tray:
            self.tray.stop()
        if self.mouse_listener:
            self.mouse_listener.stop()
        self.root.destroy()
        os._exit(0)

    def run(self):
        self.root.mainloop()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI: coords match
    except Exception:
        user32.SetProcessDPIAware()
    single_instance_or_exit()
    App().run()


if __name__ == "__main__":
    main()
