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

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

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
    "system_prompt": "Explain the highlighted text clearly and concisely.",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    cfg["base_url"] = cfg["base_url"].rstrip("/")
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


def stream_chat(messages, model, cancel):
    """Yield content chunks from LM Studio's streaming chat endpoint."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": CFG["temperature"],
        "max_tokens": CFG["max_tokens"],
        "stream": True,
    }).encode("utf-8")
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
class ExplainWindow:
    def __init__(self, root, selected, x, y):
        self.root = root
        self.selected = selected
        self.cancel = threading.Event()
        self.q = queue.Queue()
        self.busy = False
        self.messages = [
            {"role": "system", "content": CFG["system_prompt"]},
            {"role": "user", "content": f"Explain this text:\n\n\"\"\"\n{selected}\n\"\"\""},
        ]
        self.segments = []      # finished (kind, text) for display
        self.raw = ""           # current answer, raw
        self.model = None

        w = self.win = tk.Toplevel(root)
        w.title("Explain")
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.bind("<Escape>", lambda e: self.close())

        fs = CFG["font_size"]
        base = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self.f_body = tkfont.Font(family=base, size=fs)
        self.f_bold = tkfont.Font(family=base, size=fs, weight="bold")
        self.f_head = tkfont.Font(family=base, size=fs + 1, weight="bold")
        self.f_code = tkfont.Font(family="Consolas", size=fs)
        self.f_small = tkfont.Font(family=base, size=fs - 2)

        top = tk.Frame(w, padx=10, pady=6)
        top.pack(fill="x")
        preview = " ".join(selected.split())
        if len(preview) > 140:
            preview = preview[:140] + "…"
        tk.Label(top, text=f"“{preview}”", font=self.f_small, fg="#555",
                 wraplength=520, justify="left", anchor="w").pack(fill="x")

        mid = tk.Frame(w, padx=10)
        mid.pack(fill="both", expand=True)
        sb = tk.Scrollbar(mid)
        sb.pack(side="right", fill="y")
        t = self.text = tk.Text(mid, wrap="word", font=self.f_body, relief="flat",
                                padx=6, pady=6, bg="#fbfbfb", yscrollcommand=sb.set,
                                height=16, width=64, cursor="arrow")
        t.pack(side="left", fill="both", expand=True)
        sb.config(command=t.yview)
        t.tag_configure("bold", font=self.f_bold)
        t.tag_configure("head", font=self.f_head, spacing1=4)
        t.tag_configure("code", font=self.f_code, background="#eeeeee")
        t.tag_configure("you", font=self.f_bold, foreground="#1a5fb4", spacing1=8)
        t.tag_configure("muted", foreground="#888")
        t.tag_configure("error", foreground="#b00020")
        # read-only but still selectable/copyable
        t.bind("<Key>", lambda e: None if (e.state & 0x4 and e.keysym.lower() in ("c", "a")) else "break")

        bottom = tk.Frame(w, padx=10, pady=8)
        bottom.pack(fill="x")
        self.entry = tk.Entry(bottom, font=self.f_body)
        self.entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.entry.bind("<Return>", lambda e: self.follow_up())
        self.ask_btn = tk.Button(bottom, text="Ask", width=6, command=self.follow_up)
        self.ask_btn.pack(side="left", padx=(6, 0))
        tk.Button(bottom, text="Copy", width=6, command=self.copy_answer).pack(side="left", padx=(6, 0))
        tk.Button(bottom, text="Close", width=6, command=self.close).pack(side="left", padx=(6, 0))

        self.status = tk.Label(w, text="", font=self.f_small, fg="#777", anchor="w", padx=12)
        self.status.pack(fill="x", pady=(0, 6))

        self.place(x, y)
        # Windows applies the launcher's "start minimized/hidden" flag to the first window
        # a process shows; force normal state so the popup is never born minimized.
        w.deiconify()
        w.after(30, lambda: w.state("normal") if w.state() == "iconic" else None)
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

    # ---- requests
    def start_request(self):
        self.busy = True
        self.raw = ""
        self.ask_btn.config(state="disabled")
        self.status.config(text="Contacting LM Studio…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            if self.model is None:
                self.model = resolve_model()
            self.q.put(("status", f"{self.model}: waiting for first token (may load the model)…"))
            for kind, chunk in stream_chat(self.messages, self.model, self.cancel):
                self.q.put((kind, chunk))
            self.q.put(("done", None))
        except urllib.error.URLError as e:
            self.q.put(("error", "Could not reach LM Studio at "
                        f"{CFG['base_url']}.\nIn LM Studio open the Developer tab and start the "
                        f"server, then try again.\n\n({e.reason})"))
        except Exception as e:
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def poll(self):
        if self.cancel.is_set():
            return
        changed = False
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "content":
                    self.raw += val
                    changed = True
                    self.status.config(text=f"{self.model}: writing…")
                elif kind == "thinking":
                    self.status.config(text=f"{self.model}: thinking…")
                elif kind == "status":
                    self.status.config(text=val)
                elif kind == "done":
                    answer = visible_text(self.raw).strip()
                    self.messages.append({"role": "assistant", "content": answer})
                    self.segments.append(("answer", answer))
                    self.raw = ""
                    self.busy = False
                    self.ask_btn.config(state="normal")
                    self.status.config(text=f"{self.model} · Esc to close · type a follow-up below")
                    changed = True
                elif kind == "error":
                    self.segments.append(("error", val))
                    if self.messages[-1]["role"] == "user" and len(self.messages) > 2:
                        self.messages.pop()  # let the user retry the follow-up
                    self.raw = ""
                    self.busy = False
                    self.ask_btn.config(state="normal")
                    self.status.config(text="Error")
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self.render()
        self.win.after(50, self.poll)

    def follow_up(self):
        q = self.entry.get().strip()
        if not q or self.busy:
            return
        self.entry.delete(0, "end")
        self.messages.append({"role": "user", "content": q})
        self.segments.append(("you", q))
        self.render()
        self.start_request()

    # ---- display
    def render(self):
        t = self.text
        at_bottom = t.yview()[1] > 0.98
        t.delete("1.0", "end")
        for kind, txt in self.segments:
            if kind == "you":
                t.insert("end", "\nYou: " + txt + "\n", "you")
            elif kind == "error":
                t.insert("end", "\n" + txt + "\n", "error")
            else:
                self.insert_markdown(txt)
        if self.busy and self.raw:
            self.insert_markdown(visible_text(self.raw))
        if at_bottom or self.busy:
            t.see("end")

    def insert_markdown(self, md):
        """Very small markdown renderer: headings, bullets, **bold**, `code`, ``` blocks."""
        t = self.text
        in_code = False
        for line in md.split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                t.insert("end", line + "\n", "code")
                continue
            m = re.match(r"^\s{0,3}#{1,6}\s+(.*)", line)
            if m:
                t.insert("end", m.group(1).replace("**", "") + "\n", "head")
                continue
            m = re.match(r"^(\s*)[*\-+]\s+(.*)", line)
            if m:
                t.insert("end", "  " * (len(m.group(1)) // 2) + "• ")
                line = m.group(2)
            for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", line):
                if part.startswith("**") and part.endswith("**") and len(part) > 4:
                    t.insert("end", part[2:-2], "bold")
                elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                    t.insert("end", part[1:-1], "code")
                else:
                    t.insert("end", part)
            t.insert("end", "\n")

    def copy_answer(self):
        answers = [txt for kind, txt in self.segments if kind == "answer"]
        if answers:
            self.root.clipboard_clear()
            self.root.clipboard_append(answers[-1])
            self.status.config(text="Answer copied to clipboard")

    def close(self):
        self.cancel.set()
        self.win.destroy()


# ------------------------------------------------------------------- triggers
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
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

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(26, 95, 180, 255))
        try:
            from PIL import ImageFont
            fnt = ImageFont.truetype("segoeuib.ttf", 40)
        except Exception:
            fnt = None
        d.text((32, 32), "?", fill="white", font=fnt, anchor="mm")
        menu = pystray.Menu(
            pystray.MenuItem("Explain clipboard text", lambda: self.trigger_q.put("clipboard")),
            pystray.MenuItem(f"Trigger: Ctrl+Right-click or {CFG.get('hotkey')}", None, enabled=False),
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
                _, text, x, y = item
                if not text:
                    self._toast("No text was selected (or the program blocked copying).", x, y)
                else:
                    ExplainWindow(self.root, text, x, y)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _toast(self, msg, x, y):
        tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=msg, bg="#333", fg="white", padx=10, pady=6).pack()
        tw.geometry(f"+{x + 12}+{y + 12}")
        tw.after(1800, tw.destroy)

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
