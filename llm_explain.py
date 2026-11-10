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
    "max_tabs": 8,
    "always_on_top": True,
    "reasoning_allowance": 2500,
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


# ------------------------------------------------------------------ providers
# A provider is a dict: {"name", "type", "base_url", "model", optional "api_key_env", "effort"}
#   type "lmstudio":  LM Studio (root URL, e.g. http://localhost:1234). Auto model, unload, TTL.
#   type "openai":    any OpenAI-compatible API (base URL incl. version, e.g. .../v1).
#   type "anthropic": Claude through the official Anthropic SDK.
# API keys are kept in Windows Credential Manager, never in config.json.
PROVIDER_TYPES = {"lmstudio": "LM Studio", "openai": "OpenAI-compatible", "anthropic": "Anthropic (Claude)"}

PRESETS = [
    {"name": "LM Studio", "type": "lmstudio", "base_url": "http://localhost:1234", "model": ""},
    {"name": "Anthropic (Claude)", "type": "anthropic", "base_url": "", "model": "claude-opus-5-5"},
    {"name": "OpenAI", "type": "openai", "base_url": "https://api.openai.com/v1", "model": ""},
    {"name": "Google Gemini", "type": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": ""},
    {"name": "OpenRouter", "type": "openai", "base_url": "https://openrouter.ai/api/v1", "model": ""},
    {"name": "Groq", "type": "openai", "base_url": "https://api.groq.com/openai/v1", "model": ""},
    {"name": "Mistral", "type": "openai", "base_url": "https://api.mistral.ai/v1", "model": ""},
    {"name": "DeepSeek", "type": "openai", "base_url": "https://api.deepseek.com/v1", "model": ""},
    {"name": "Ollama (local)", "type": "openai", "base_url": "http://localhost:11434/v1", "model": ""},
    {"name": "Custom (OpenAI-compatible)", "type": "openai", "base_url": "", "model": ""},
]


def providers():
    """The configured providers. Older configs (LM Studio only) are converted on the fly."""
    lst = CFG.get("providers")
    if not isinstance(lst, list) or not lst:
        lst = [{"name": "LM Studio", "type": "lmstudio",
                "base_url": CFG.get("base_url", "http://localhost:1234"),
                "model": CFG.get("model", "")}]
        CFG["providers"] = lst
    return lst


def get_provider(name=None):
    lst = providers()
    name = name or CFG.get("provider")
    return next((p for p in lst if p.get("name") == name), lst[0])


def norm_url(url):
    # "localhost" makes Windows try IPv6 (::1) first; local servers usually listen on IPv4,
    # so every request would wait ~2 s for the fallback. Go straight to 127.0.0.1.
    return (url or "").strip().rstrip("/").replace("://localhost", "://127.0.0.1")


def lmstudio_provider():
    """The LM Studio provider to use for unloading (the active one if it is LM Studio)."""
    p = get_provider()
    if p.get("type") == "lmstudio":
        return p
    return next((q for q in providers() if q.get("type") == "lmstudio"), None)


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


def api_key(prov):
    """Saved key, else the environment variable named in "api_key_env" (if any)."""
    if prov.get("_key"):  # typed in Settings but not saved yet (Test / Fetch)
        return prov["_key"]
    k = key_get(prov.get("name", ""))
    if not k and prov.get("api_key_env"):
        k = os.environ.get(prov["api_key_env"], "")
    return k


# ---- HTTP helpers
def _get_json(path, timeout=5, base=None, headers=None):
    base = norm_url(base or get_provider().get("base_url"))
    req = urllib.request.Request(base + path, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_error_text(e):
    """Readable message from an HTTPError, using the JSON error body when there is one."""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    msg = body
    try:
        err = json.loads(body).get("error", body)
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except Exception:
        pass
    hint = {401: "Check the API key in Settings.", 403: "The API key isn't allowed to do this.",
            404: "Check the base URL and model name in Settings.",
            429: "Rate limit or quota reached. Wait a moment or check your account."}.get(e.code, "")
    return f"HTTP {e.code}: {msg.strip()[:300]}" + (f"\n{hint}" if hint else "")


def list_models(prov):
    """Model ids a provider offers (for the Settings "Fetch" button)."""
    t = prov.get("type")
    if t == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key(prov) or None,
                                     base_url=norm_url(prov.get("base_url")) or None)
        return [m.id for m in client.models.list()]
    key = api_key(prov)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    path = "/v1/models" if t == "lmstudio" else "/models"
    data = _get_json(path, timeout=15, base=prov.get("base_url"), headers=headers)
    ids = [m.get("id") for m in data.get("data", data.get("models", [])) if isinstance(m, dict)]
    return sorted(i.replace("models/", "") for i in ids if i and "embed" not in i.lower())


def resolve_model(prov=None):
    """LM Studio: configured model > one already loaded > fallback > first chat model.
    Other providers: the model set in Settings."""
    prov = prov or get_provider()
    if prov.get("model"):
        return prov["model"]
    if prov.get("type") != "lmstudio":
        raise RuntimeError(f"No model is set for \"{prov.get('name')}\". "
                           "Open Settings (the provider menu at the top right) and choose one.")
    base = prov.get("base_url")
    fallback = CFG.get("fallback_model")
    try:
        models = _get_json("/api/v0/models", base=base).get("data", [])
        chat = [m for m in models if m.get("type") in ("llm", "vlm")]
        loaded = [m for m in chat if m.get("state") == "loaded"]
        if loaded:
            return loaded[0]["id"]
        if fallback:
            return fallback
        if chat:
            return chat[0]["id"]
    except Exception:
        pass
    if fallback:
        return fallback
    models = _get_json("/v1/models", base=base).get("data", [])
    ids = [m["id"] for m in models if "embed" not in m["id"].lower()]
    if not ids:
        raise RuntimeError("No chat model found in LM Studio.")
    return ids[0]


def stream_chat(messages, model, cancel, max_tokens=None, prov=None):
    """Yield ("content", text) / ("thinking", "") chunks from the provider's streaming API."""
    prov = prov or get_provider()
    if prov.get("type") == "anthropic":
        yield from _stream_anthropic(messages, model, cancel, prov)
    else:
        yield from _stream_openai(messages, model, cancel, max_tokens, prov)


def _stream_openai(messages, model, cancel, max_tokens, prov):
    """OpenAI-compatible /chat/completions (LM Studio, OpenAI, Gemini, OpenRouter, Ollama...)."""
    is_lms = prov.get("type") == "lmstudio"
    base = norm_url(prov.get("base_url"))
    url = base + ("/v1/chat/completions" if is_lms else "/chat/completions")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": CFG["temperature"],
        "max_tokens": max_tokens or CFG["max_tokens"],
        "stream": True,
    }
    if is_lms and CFG.get("auto_unload_minutes"):
        # LM Studio unloads a model it auto-loaded after this many idle seconds
        payload["ttl"] = int(float(CFG["auto_unload_minutes"]) * 60)
    headers = {"Content-Type": "application/json"}
    key = api_key(prov)
    if key:
        headers["Authorization"] = f"Bearer {key}"

    resp = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            # Generous timeout: the first request may have to load a local model into memory.
            resp = urllib.request.urlopen(req, timeout=600)
            break
        except urllib.error.HTTPError as e:
            text = _http_error_text(e)
            low = text.lower()
            # Some models only accept their defaults: adapt and retry once per parameter.
            if e.code == 400 and "max_tokens" in low and "max_tokens" in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                continue
            if e.code == 400 and "temperature" in low and "temperature" in payload:
                payload.pop("temperature")
                continue
            raise RuntimeError(text) from None
    if resp is None:
        raise RuntimeError("The API rejected the request parameters.")
    with resp:
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
                err = obj["error"]
                raise RuntimeError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
            choices = obj.get("choices") or [{}]
            delta = choices[0].get("delta", {}) or {}
            if delta.get("reasoning_content") or delta.get("reasoning"):
                yield ("thinking", "")
            if delta.get("content"):
                yield ("content", delta["content"])


# Models that support server-side refusal fallbacks ("default" routes by refusal category)
_CLAUDE_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5",
                           "claude-opus-5", "claude-sonnet-5-5"}


def _stream_anthropic(messages, model, cancel, prov):
    """Claude via the official Anthropic SDK (Messages API, streaming)."""
    key = api_key(prov)
    if not key:
        raise RuntimeError(f"No API key saved for \"{prov.get('name')}\". Open Settings "
                           "(the provider menu at the top right) and add your Anthropic API key.")
    import anthropic  # loaded on first use (~2 s); App preloads it when Claude is set up
    client = anthropic.Anthropic(api_key=key, base_url=norm_url(prov.get("base_url")) or None)
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [m for m in messages if m["role"] != "system"]
    # Length is set by the prompt; max_tokens is only a safety ceiling (thinking counts too).
    kwargs = dict(model=model, max_tokens=16000, system=system, messages=convo)
    if prov.get("effort"):
        kwargs["output_config"] = {"effort": prov["effort"]}
    if model in _CLAUDE_FALLBACK_MODELS:
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        streamer = client.beta.messages.stream
    else:
        streamer = client.messages.stream
    try:
        with streamer(**kwargs) as stream:
            for event in stream:
                if cancel.is_set():
                    return
                if event.type == "content_block_start" and event.content_block.type == "thinking":
                    yield ("thinking", "")
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield ("content", event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        yield ("thinking", "")
            final = stream.get_final_message()
        if final.stop_reason == "refusal":
            raise RuntimeError("Claude declined to answer this one.")
    except anthropic.AuthenticationError:
        raise RuntimeError("Anthropic rejected the API key. Check it in Settings.") from None
    except anthropic.NotFoundError:
        raise RuntimeError(f"Anthropic doesn't know the model \"{model}\". Check it in Settings.") from None
    except anthropic.RateLimitError:
        raise RuntimeError("Anthropic rate limit reached. Wait a moment and press Regenerate.") from None
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error {e.status_code}: {e.message}") from None
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't connect to Anthropic. Check your internet connection.") from None


# ---- LM Studio model unloading
def loaded_models(base=None):
    """[(display name, instance id)] of models currently loaded in LM Studio."""
    data = _get_json("/api/v1/models", base=base)
    return [(m.get("display_name") or m.get("key"), inst["id"])
            for m in data.get("models", []) for inst in m.get("loaded_instances", [])]


def _lms_cli():
    path = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms.exe")
    return path if os.path.exists(path) else None


def unload_models():
    """Unload every model loaded in LM Studio. Returns a short message for the user."""
    prov = lmstudio_provider()
    if not prov:
        return "No LM Studio provider is set up."
    base = prov.get("base_url")
    try:
        loaded = loaded_models(base)
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
        req = urllib.request.Request(norm_url(base) + "/api/v1/models/unload",
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


class ExplainTab:
    """One lookup: its conversation, request state, and its widgets inside the window."""

    def __init__(self, win, selected):
        self.win = win
        self.selected = selected
        self.closed = False
        self.q = queue.Queue()
        self.busy = False
        self.unloading = False
        self.rid = 0                         # id of the current request
        self.req_cancel = threading.Event()  # cancels the current request
        self.tick = 0
        self.status_text = ""
        self.length = CFG.get("length", "standard")
        if self.length not in LENGTHS:
            self.length = "standard"
        # the system prompt is added per request, so the length can change
        self.messages = [{"role": "user",
                          "content": f"Explain this text:\n\n\"\"\"\n{selected}\n\"\"\""}]
        self.segments = []   # finished (kind, text) for display
        self.raw = ""        # answer being streamed, raw
        self.model = None
        self.thought = False
        self.provider_name = get_provider()["name"]  # follow-ups use the same provider

        p, px = win.pal, win.px
        flat = " ".join(selected.split())
        self.title = flat[:60]
        preview = flat if len(flat) <= 180 else flat[:180] + "…"
        self.quote = tk.Frame(win.quote_holder, bg=p["subtle"])
        tk.Frame(self.quote, bg=p["accent"], width=px(3)).pack(side="left", fill="y")
        tk.Label(self.quote, text=preview, font=win.f_quote, bg=p["subtle"], fg=p["muted"],
                 wraplength=px(330), justify="left", anchor="w",
                 padx=px(10), pady=px(7)).pack(side="left", fill="x", expand=True)
        self.text = win.make_text()

    # ---- state shown by the window
    def set_status(self, text):
        self.status_text = text
        if self.win.active is self:
            self.win.status.config(text=text)

    def _changed(self):
        self.win.tab_changed(self)

    # ---- requests
    def start_request(self):
        self.req_cancel.set()                # stop any answer still streaming
        self.req_cancel = threading.Event()
        self.rid += 1
        self.busy = True
        self.raw = ""
        self.set_status(f"Contacting {self.provider_name}…")
        self._changed()
        label, instr, max_tokens = LENGTHS[self.length]
        system = CFG["system_prompt"] + ("\n\n" + instr if instr else "")
        msgs = [{"role": "system", "content": system}] + list(self.messages)
        max_tokens = max(max_tokens, CFG["max_tokens"]) if self.length == "detailed" else \
            (max_tokens or CFG["max_tokens"])
        # Reasoning models (e.g. Gemma 4) "think" in hidden tokens that count against the
        # limit; without this room a short answer can come back empty.
        max_tokens += int(CFG.get("reasoning_allowance", 2500))
        self.thought = False
        threading.Thread(target=self._worker, args=(self.rid, self.req_cancel, msgs, max_tokens),
                         daemon=True).start()
        self.render()

    @property
    def provider(self):
        return get_provider(self.provider_name)

    def _worker(self, rid, cancel, msgs, max_tokens):
        put = lambda kind, val=None: self.q.put((rid, kind, val))
        prov = self.provider
        try:
            if self.model is None:
                self.model = resolve_model(prov)
            put("status", f"{self.model} · waiting for the model…")
            for kind, chunk in stream_chat(msgs, self.model, cancel, max_tokens, prov):
                put(kind, chunk)
            if not cancel.is_set():
                put("done")
        except urllib.error.HTTPError as e:
            put("error", _http_error_text(e))
        except urllib.error.URLError as e:
            if prov.get("type") == "lmstudio":
                put("error", f"Could not reach LM Studio at {prov.get('base_url')}.\n"
                    "In LM Studio open the Developer tab and start the server, then press "
                    f"Regenerate.\n\n({e.reason})")
            else:
                put("error", f"Could not reach {prov.get('name')} at {prov.get('base_url')}.\n"
                    "Check your internet connection and the base URL in Settings.\n\n"
                    f"({e.reason})")
        except RuntimeError as e:
            put("error", str(e))
        except Exception as e:
            put("error", f"{type(e).__name__}: {e}")

    def _idle(self, text):
        self.busy = False
        self.set_status(text)
        self._changed()

    def poll_once(self):
        changed = False
        try:
            while True:
                rid, kind, val = self.q.get_nowait()
                if kind == "unloaded":
                    self.unloading = False
                    self.set_status(val.split(" · ")[0])  # short form fits the row
                    self._changed()
                    continue
                if rid != self.rid:
                    continue  # from a cancelled request
                if kind == "content":
                    self.raw += val
                    changed = True
                    self.set_status(f"{self.model} · writing…")
                elif kind == "thinking":
                    self.thought = True
                    self.set_status(f"{self.model} · thinking…")
                elif kind == "status":
                    self.set_status(val)
                elif kind == "done" and not visible_text(self.raw).strip():
                    self.segments.append(("error", (
                        "The model used its whole token limit thinking and gave no answer. "
                        "Raise reasoning_allowance in config.json."
                        if self.thought else "The model returned an empty answer. Try Regenerate.")))
                    self.raw = ""
                    self._idle("No answer")
                    changed = True
                elif kind == "done":
                    answer = visible_text(self.raw).strip()
                    self.messages.append({"role": "assistant", "content": answer})
                    self.segments.append(("answer", answer))
                    self.raw = ""
                    self._idle(f"{self.model} · {LENGTHS[self.length][0].lower()}")
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

    def follow_up(self, q):
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
            self.busy = False
            self.render()
        self.unloading = True
        self.set_status("Unloading model…")
        self._changed()

        def work():
            self.q.put((None, "unloaded", unload_models_safe()))
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
        save_setting("length", key)  # new tabs start with the last choice
        self.regenerate()

    def set_provider(self, name):
        """Answer this tab again with another provider (also the default for new tabs)."""
        save_setting("provider", name)
        if name == self.provider_name:
            return
        self.provider_name = name
        self.model = None
        self.regenerate()

    def copy_answer(self):
        answers = [txt for kind, txt in self.segments if kind == "answer"]
        if answers:
            self.win.root.clipboard_clear()
            self.win.root.clipboard_append(answers[-1])
            self.set_status("Copied to clipboard ✓")

    def close(self):
        self.closed = True
        self.req_cancel.set()
        self.quote.destroy()
        self.text.destroy()

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


class ExplainWindow:
    """The single window. Each lookup is a tab; closing the window only hides it."""

    def __init__(self, root):
        self.root = root
        self.tabs = []
        self.active = None
        self.sized = False
        self.styled = False

        p, dark = current_theme()
        self.pal, self.dark = p, dark
        w = self.win = tk.Toplevel(root, bg=p["bg"])
        w.withdraw()
        w.title("LLM Explain")
        if CFG.get("always_on_top", True):
            w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.hide)
        w.bind("<Escape>", lambda e: self.hide())
        w.bind("<Control-w>", lambda e: self.active and self.close_tab(self.active))
        w.bind("<Control-W>", lambda e: self.active and self.close_tab(self.active))
        w.bind("<Control-Tab>", lambda e: (self.cycle(1), "break")[1])
        w.bind("<Control-Shift-Tab>", lambda e: (self.cycle(-1), "break")[1])
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
        self.f_code = tkfont.Font(family=mono, size=fs - 1)
        self.f_small = tkfont.Font(family=ui, size=fs - 2)
        self.f_small_b = tkfont.Font(family=ui, size=fs - 2, weight="bold")
        self.f_tab = tkfont.Font(family=ui, size=fs - 1)
        self.f_quote = tkfont.Font(family=ui, size=fs - 1, slant="italic")
        self.f_italic = tkfont.Font(family=ui, size=fs, slant="italic")

        # ---- tab bar
        self.tabbar = tk.Frame(w, bg=p["subtle"], height=px(40))
        self.tabbar.pack(fill="x")
        self.tabbar.pack_propagate(False)
        self.prov_btn = FlatButton(self.tabbar, "", self._provider_menu, p, self.f_small,
                                   padx=px(10), pady=px(4))
        self.prov_btn.pack(side="right", padx=(0, px(8)), pady=(px(7), 0))
        self.tabs_frame = tk.Frame(self.tabbar, bg=p["subtle"])
        self.tabs_frame.pack(side="left", fill="both", expand=True, padx=px(8))
        self.tabs_frame.pack_propagate(False)
        self.tabs_frame.bind("<Configure>", lambda e: self._layout_tabs())

        # ---- quote of the active tab + length switch
        top = tk.Frame(w, bg=p["bg"])
        top.pack(fill="x", padx=px(18), pady=(px(12), px(8)))
        self.seg = Segmented(top, [(k, LENGTHS[k][0]) for k in LENGTH_ORDER],
                             CFG.get("length", "standard"), self.set_length, p,
                             self.f_small, self.f_small_b, px)
        self.seg.pack(side="right", anchor="n", padx=(px(10), 0))
        self.quote_holder = tk.Frame(top, bg=p["bg"])
        self.quote_holder.pack(side="left", fill="x", expand=True)

        # ---- answer pages (one Text per tab, only the active one is shown)
        self.pages = tk.Frame(w, bg=p["bg"])
        self.pages.pack(fill="both", expand=True)

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
        self.copy_btn = FlatButton(srow, "Copy", lambda: self.active and self.active.copy_answer(),
                                   p, self.f_small, padx=px(9), pady=px(3))
        self.copy_btn.pack(side="right")
        self.regen_btn = FlatButton(srow, "Regenerate",
                                    lambda: self.active and self.active.regenerate(),
                                    p, self.f_small, padx=px(9), pady=px(3))
        self.regen_btn.pack(side="right", padx=(0, px(4)))
        self.unload_btn = FlatButton(srow, "Unload model",
                                     lambda: self.active and self.active.unload(),
                                     p, self.f_small, padx=px(9), pady=px(3))
        self.unload_btn.pack(side="right", padx=(0, px(4)))

        w.minsize(px(420), px(300))
        self.poll()

    # ---- answer text widget for a tab
    def make_text(self):
        p, px = self.pal, self.px
        t = tk.Text(
            self.pages, wrap="word", font=self.f_body, relief="flat", bd=0, highlightthickness=0,
            bg=p["bg"], fg=p["text"], padx=px(18), pady=px(6), width=58, height=13,
            cursor="arrow", insertwidth=0, selectbackground=p["accent_soft"],
            selectforeground=p["text"], inactiveselectbackground=p["accent_soft"],
            spacing1=px(1), spacing2=px(3), spacing3=px(3))
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
        return t

    # ---- tabs
    def add_tab(self, selected, x, y):
        tab = ExplainTab(self, selected)
        self.tabs.append(tab)
        self._make_tab_button(tab)
        max_tabs = max(1, int(CFG.get("max_tabs", 8)))
        while len(self.tabs) > max_tabs:  # close the oldest finished tab
            old = next((t for t in self.tabs if not t.busy and t is not tab), self.tabs[0])
            self.close_tab(old, hide_if_empty=False)
        self._repack_tabs()
        self.select(tab)
        tab.start_request()
        if self.win.state() == "withdrawn":
            self.show(x, y)
        else:
            self.bring_to_front()

    def _make_tab_button(self, tab):
        p, px = self.pal, self.px
        f = tk.Frame(self.tabs_frame, height=px(32), cursor="hand2")
        f.pack_propagate(False)
        x = tk.Label(f, text="✕", font=self.f_small, cursor="hand2", padx=px(4))
        x.pack(side="right", padx=(0, px(6)))
        lbl = tk.Label(f, font=self.f_tab, anchor="w", width=1, cursor="hand2")
        lbl.pack(side="left", fill="both", expand=True, padx=(px(12), px(2)))
        tab.btn, tab.btn_label, tab.btn_x = f, lbl, x
        tab.hover = False

        def enter(e):
            tab.hover = True
            self._paint_tab(tab)

        def leave(e):
            under = self.win.winfo_containing(*self.win.winfo_pointerxy())
            if under not in (f, lbl, x):
                tab.hover = False
                self._paint_tab(tab)

        for wdg in (f, lbl, x):
            wdg.bind("<Enter>", enter)
            wdg.bind("<Leave>", leave)
            wdg.bind("<Button-2>", lambda e, t=tab: self.close_tab(t))  # middle-click
        for wdg in (f, lbl):
            wdg.bind("<Button-1>", lambda e, t=tab: self.select(t))
        x.bind("<Button-1>", lambda e, t=tab: self.close_tab(t))
        x.bind("<Enter>", lambda e: (enter(e), x.config(fg=p["text"])), add="+")
        x.bind("<Leave>", lambda e: x.config(fg=p["muted"]), add="+")
        self._paint_tab(tab)

    def _paint_tab(self, tab):
        p = self.pal
        if tab is self.active:
            bg, fg = p["bg"], p["text"]
        elif tab.hover:
            bg, fg = p["hover"], p["text"]
        else:
            bg, fg = p["subtle"], p["muted"]
        tab.btn.config(bg=bg)
        tab.btn_label.config(bg=bg, fg=fg, text=("● " if tab.busy else "") + tab.title)
        tab.btn_x.config(bg=bg, fg=p["muted"])

    def _repack_tabs(self):
        for t in self.tabs:
            t.btn.pack_forget()
        for t in self.tabs:
            t.btn.pack(side="left", padx=(0, self.px(2)), pady=(self.px(8), 0))
        self._layout_tabs()

    def _layout_tabs(self):
        """Browser-style: tabs share the bar and shrink as more are opened."""
        if not self.tabs:
            return
        avail = self.tabs_frame.winfo_width()
        if avail <= 1:
            avail = self.px(520)
        gap = self.px(2)
        width = min(self.px(190), max(self.px(64), (avail - gap * len(self.tabs)) // len(self.tabs)))
        for t in self.tabs:
            t.btn.config(width=width)

    def select(self, tab):
        if self.active is not None and self.active is not tab and not self.active.closed:
            self.active.quote.pack_forget()
            self.active.text.pack_forget()
        prev, self.active = self.active, tab
        tab.quote.pack(fill="x")
        tab.text.pack(fill="both", expand=True)
        for t in self.tabs:
            self._paint_tab(t)
        self.refresh_controls()

    def close_tab(self, tab, hide_if_empty=True):
        if tab not in self.tabs:
            return
        i = self.tabs.index(tab)
        was_active = tab is self.active
        tab.close()
        tab.btn.destroy()
        self.tabs.remove(tab)
        if was_active:
            self.active = None
            if self.tabs:
                self.select(self.tabs[min(i, len(self.tabs) - 1)])
        if not self.tabs:
            self.refresh_controls()
            if hide_if_empty:
                self.hide()
        self._layout_tabs()

    def cycle(self, step):
        if len(self.tabs) > 1 and self.active in self.tabs:
            i = self.tabs.index(self.active)
            self.select(self.tabs[(i + step) % len(self.tabs)])

    def tab_changed(self, tab):
        if tab in self.tabs:
            self._paint_tab(tab)
        if tab is self.active:
            self.refresh_controls()

    def _provider_menu(self):
        p = self.pal
        m = tk.Menu(self.win, tearoff=0, bg=p["bg"], fg=p["text"], activebackground=p["accent_soft"],
                    activeforeground=p["text"], selectcolor=p["accent"], font=self.f_small, bd=0)
        self._prov_var = tk.StringVar(value=self.active.provider_name if self.active
                                      else get_provider()["name"])
        for prov in providers():
            name = prov["name"]
            m.add_radiobutton(label=f"{name}   ·   {prov.get('model') or 'auto'}",
                              variable=self._prov_var, value=name,
                              command=lambda n=name: self.set_provider(n))
        m.add_separator()
        m.add_command(label="Settings…", command=self.open_settings)
        self._menu = m
        m.tk_popup(self.prov_btn.winfo_rootx(),
                   self.prov_btn.winfo_rooty() + self.prov_btn.winfo_height())

    def set_provider(self, name):
        if self.active:
            self.active.set_provider(name)
        else:
            save_setting("provider", name)
        self.refresh_controls()

    def open_settings(self):
        SettingsDialog(self.root, on_save=self.on_settings_saved)

    def on_settings_saved(self, renames):
        for t in self.tabs:
            if t.provider_name in renames:
                t.provider_name = renames[t.provider_name]
            t.model = None  # settings may have changed the model
        self.refresh_controls()

    def refresh_controls(self):
        t = self.active
        self.send_btn.set_enabled(bool(t) and not t.busy)
        self.regen_btn.set_enabled(bool(t) and not t.busy)
        name = t.provider_name if t else get_provider()["name"]
        self.prov_btn.config(text=f"{name}  ▾")
        is_lms = get_provider(name).get("type") == "lmstudio"
        self.unload_btn.set_enabled(bool(t) and not t.unloading and is_lms)
        self.copy_btn.set_enabled(bool(t))
        self.status.config(text=t.status_text if t else "")
        if t:
            self.seg.set(t.length)

    # ---- actions on the active tab
    def follow_up(self):
        q = self.entry_var.get().strip()
        if not q or not self.active or self.active.busy:
            return
        self.entry_var.set("")
        self.active.follow_up(q)

    def set_length(self, key):
        if self.active:
            self.active.set_length(key)
            self.seg.set(key)

    def _placeholder(self):
        if self.entry_var.get():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(in_=self.entry, x=0, rely=0.5, anchor="w")

    # ---- window
    def show(self, x, y):
        """Show the window near (x, y)."""
        w = self.win
        w.update_idletasks()
        if not self.sized:
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
        else:
            ww, wh = w.winfo_width(), w.winfo_height()
        left, top, right, bottom = work_area_at(x, y)
        nx = min(max(left, x + 16), right - ww - 8)
        ny = max(top, min(max(top, y + 16), bottom - wh - 40))
        # Fixing the size stops the window resizing when tabs change.
        w.geometry(f"{ww}x{wh}+{nx}+{ny}")
        self.sized = True
        self.bring_to_front()

    def bring_to_front(self):
        w = self.win
        # Windows applies the launcher's "start minimized/hidden" flag to the first window
        # a process shows; force normal state so the window is never born minimized.
        w.deiconify()
        w.after(30, lambda: w.state("normal") if w.state() == "iconic" else None)
        if not self.styled:
            style_titlebar(w, self.pal, self.dark)
            self.styled = True
        w.lift()
        w.focus_force()
        self.entry.focus_set()

    def show_existing(self):
        if self.tabs:
            self.bring_to_front()
            return True
        return False

    def hide(self):
        self.win.withdraw()

    def poll(self):
        for t in list(self.tabs):
            t.poll_once()
        self.win.after(50, self.poll)


class SettingsDialog:
    """Add, edit, test and delete model providers. API keys go to Windows Credential Manager."""
    _open = None

    TYPE_HINTS = {
        "lmstudio": "Local LM Studio server (root URL, no /v1). Leave the model empty to use "
                    "whatever is loaded. No API key needed.",
        "openai": "Any OpenAI-compatible API. The base URL includes the version, e.g. "
                  "https://api.openai.com/v1. Press Fetch to pick a model.",
        "anthropic": "Claude, through Anthropic's official SDK. Get an API key at "
                     "console.anthropic.com. Leave the base URL empty.",
    }

    def __init__(self, root, on_save=None):
        if SettingsDialog._open and SettingsDialog._open.win.winfo_exists():
            SettingsDialog._open.win.deiconify()
            SettingsDialog._open.win.lift()
            SettingsDialog._open.win.focus_force()
            return
        SettingsDialog._open = self
        self.root, self.on_save = root, on_save
        self.items = [dict(x, _orig=x.get("name")) for x in providers()]  # working copies
        self.deleted = []
        self.cur = None
        self.q = queue.Queue()

        p, dark = current_theme()
        self.pal = p
        w = self.win = tk.Toplevel(root, bg=p["bg"])
        w.withdraw()
        w.title("LLM Explain · Settings")
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.bind("<Escape>", lambda e: self.close())
        scale = w.winfo_fpixels("1i") / 96.0
        px = self.px = lambda v: max(1, int(round(v * scale)))
        fs = CFG["font_size"]
        ui = pick_font(root, "Segoe UI Variable Text", "Segoe UI", "TkDefaultFont")
        ui_disp = pick_font(root, "Segoe UI Variable Display", "Segoe UI", "TkDefaultFont")
        self.f = tkfont.Font(family=ui, size=fs - 1)
        self.f_b = tkfont.Font(family=ui, size=fs - 1, weight="bold")
        self.f_small = tkfont.Font(family=ui, size=fs - 2)
        self.f_title = tkfont.Font(family=ui_disp, size=fs + 2, weight="bold")

        tk.Label(w, text="Model providers", font=self.f_title, bg=p["bg"], fg=p["text"],
                 anchor="w").pack(fill="x", padx=px(20), pady=(px(16), px(10)))
        body = tk.Frame(w, bg=p["bg"])
        body.pack(fill="both", expand=True, padx=px(20))

        # ---- provider list
        left = tk.Frame(body, bg=p["bg"])
        left.pack(side="left", fill="y")
        self.lb = tk.Listbox(left, font=self.f, bg=p["subtle"], fg=p["text"], relief="flat", bd=0,
                             highlightthickness=0, selectbackground=p["accent_soft"],
                             selectforeground=p["text"], activestyle="none", exportselection=False,
                             width=22, height=12)
        self.lb.pack(fill="y", expand=True)
        self.lb.bind("<<ListboxSelect>>", lambda e: self._select_from_list())
        lrow = tk.Frame(left, bg=p["bg"])
        lrow.pack(fill="x", pady=(px(8), 0))
        self.add_btn = FlatButton(lrow, "+ Add", self._add_menu, p, self.f, padx=px(10), pady=px(4))
        self.add_btn.pack(side="left")
        FlatButton(lrow, "Delete", self._delete, p, self.f, padx=px(10), pady=px(4)).pack(side="left", padx=(px(4), 0))

        # ---- form
        form = tk.Frame(body, bg=p["bg"])
        form.pack(side="left", fill="both", expand=True, padx=(px(18), 0))
        form.columnconfigure(1, weight=1)

        def label(r, text):
            tk.Label(form, text=text, font=self.f, bg=p["bg"], fg=p["muted"], anchor="w").grid(
                row=r, column=0, sticky="w", pady=px(5), padx=(0, px(10)))

        def entry(r, **kw):
            e = tk.Entry(form, font=self.f, relief="flat", bd=0, bg=p["subtle"], fg=p["text"],
                         insertbackground=p["text"], highlightthickness=1,
                         highlightbackground=p["border"], highlightcolor=p["accent"],
                         selectbackground=p["accent_soft"], selectforeground=p["text"], width=34, **kw)
            e.grid(row=r, column=1, sticky="ew", pady=px(5), ipady=px(4))
            return e

        label(0, "Name")
        self.e_name = entry(0)
        label(1, "Type")
        self.type_var = tk.StringVar(value="openai")
        self.type_btn = FlatButton(form, "", self._type_menu, p, self.f, padx=px(8), pady=px(4))
        self.type_btn.config(anchor="w", bg=p["subtle"])
        self.type_btn.grid(row=1, column=1, sticky="ew", pady=px(5))
        label(2, "Base URL")
        self.e_url = entry(2)
        label(3, "API key")
        self.e_key = entry(3, show="•")
        krow = tk.Frame(form, bg=p["bg"])
        krow.grid(row=4, column=1, sticky="ew")
        self.key_note = tk.Label(krow, text="", font=self.f_small, bg=p["bg"], fg=p["muted"], anchor="w")
        self.key_note.pack(side="left")
        self.forget_btn = FlatButton(krow, "Forget saved key", self._forget_key, p, self.f_small,
                                     padx=px(6), pady=px(1))
        label(5, "Model")
        mrow = tk.Frame(form, bg=p["bg"])
        mrow.grid(row=5, column=1, sticky="ew", pady=px(5))
        self.e_model = tk.Entry(mrow, font=self.f, relief="flat", bd=0, bg=p["subtle"], fg=p["text"],
                                insertbackground=p["text"], highlightthickness=1,
                                highlightbackground=p["border"], highlightcolor=p["accent"],
                                selectbackground=p["accent_soft"], selectforeground=p["text"])
        self.e_model.pack(side="left", fill="x", expand=True, ipady=px(4))
        self.fetch_btn = FlatButton(mrow, "Fetch ▾", self._fetch, p, self.f, padx=px(10), pady=px(4))
        self.fetch_btn.pack(side="left", padx=(px(6), 0))
        self.hint = tk.Label(form, text="", font=self.f_small, bg=p["bg"], fg=p["muted"],
                             anchor="w", justify="left", wraplength=px(330))
        self.hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(px(8), 0))

        # ---- bottom row
        tk.Frame(w, bg=p["border"], height=1).pack(fill="x", pady=(px(14), 0))
        bottom = tk.Frame(w, bg=p["bg"])
        bottom.pack(fill="x", padx=px(20), pady=px(12))
        self.status = tk.Label(bottom, text="", font=self.f_small, bg=p["bg"], fg=p["muted"],
                               anchor="w", justify="left", wraplength=px(330), width=1)
        self.status.pack(side="left", fill="x", expand=True)
        FlatButton(bottom, "Save", self.save, p, self.f_b, kind="accent",
                   padx=px(18), pady=px(6)).pack(side="right")
        FlatButton(bottom, "Close", self.close, p, self.f, padx=px(12), pady=px(6)).pack(side="right", padx=(0, px(6)))
        self.test_btn = FlatButton(bottom, "Test", self._test, p, self.f, padx=px(12), pady=px(6))
        self.test_btn.pack(side="right", padx=(0, px(6)))

        self._refresh_list()
        start = next((i for i, it in enumerate(self.items) if it["name"] == CFG.get("provider")), 0)
        self._load(start)

        w.update_idletasks()
        sw, sh = w.winfo_reqwidth() + px(70), w.winfo_reqheight()  # room for longer names
        left_, top_, right_, bottom_ = work_area_at(*cursor_pos())
        # a fixed size stops the dialog jumping wider when another provider is selected
        w.geometry(f"{sw}x{sh}+{left_ + (right_ - left_ - sw) // 2}+{top_ + (bottom_ - top_ - sh) // 3}")
        w.deiconify()
        style_titlebar(w, p, dark)
        w.lift()
        w.focus_force()
        self._poll()

    # ---- list handling
    def _refresh_list(self):
        self.lb.delete(0, "end")
        for it in self.items:
            self.lb.insert("end", "  " + (it.get("name") or "(unnamed)"))

    def _select_from_list(self):
        sel = self.lb.curselection()
        if sel and sel[0] != self.cur:
            self._commit()
            self._load(sel[0])

    def _load(self, i):
        self.cur = i
        if i is None or not self.items:
            return
        it = self.items[i]
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(i)
        for e, v in ((self.e_name, it.get("name", "")), (self.e_url, it.get("base_url", "")),
                     (self.e_model, it.get("model", "")), (self.e_key, it.get("_key", ""))):
            e.delete(0, "end")
            e.insert(0, v)
        self.type_var.set(it.get("type", "openai"))
        self._show_type()
        self._key_note()
        self.status.config(text="", fg=self.pal["muted"])

    def _commit(self):
        """Copy the form into the selected working item."""
        if self.cur is None or self.cur >= len(self.items):
            return
        it = self.items[self.cur]
        it["name"] = self.e_name.get().strip()
        it["type"] = self.type_var.get()
        it["base_url"] = self.e_url.get().strip()
        it["model"] = self.e_model.get().strip()
        typed = self.e_key.get().strip()
        if typed:
            it["_key"] = typed
        else:
            it.pop("_key", None)
        self.lb.delete(self.cur)
        self.lb.insert(self.cur, "  " + (it["name"] or "(unnamed)"))
        self.lb.selection_set(self.cur)

    def _form_provider(self):
        """The provider as currently typed in the form (with an unsaved key, if typed)."""
        self._commit()
        it = dict(self.items[self.cur])
        if not it.get("_key") and not it.get("_forget"):
            it["_key"] = key_get(it.get("_orig") or it["name"])
        return it

    def _key_note(self):
        it = self.items[self.cur]
        saved = bool(key_get(it.get("_orig") or "")) and not it.get("_forget")
        if it.get("type") == "lmstudio":
            text = "Usually not needed for LM Studio."
        elif saved:
            text = "A key is saved in Windows Credential Manager. Leave blank to keep it."
        else:
            text = "Stored in Windows Credential Manager, not in config.json."
        self.key_note.config(text=text)
        if saved:
            self.forget_btn.pack(side="left", padx=self.px(6))
        else:
            self.forget_btn.pack_forget()

    def _forget_key(self):
        self.items[self.cur]["_forget"] = True
        self.e_key.delete(0, "end")
        self._key_note()
        self.status.config(text="The saved key will be removed when you press Save.", fg=self.pal["muted"])

    # ---- type
    def _show_type(self):
        t = self.type_var.get()
        self.type_btn.config(text=PROVIDER_TYPES.get(t, t) + "  ▾")
        self.hint.config(text=self.TYPE_HINTS.get(t, ""))

    def _type_menu(self):
        m = self._menu()
        for t, label in PROVIDER_TYPES.items():
            m.add_radiobutton(label=label, variable=self.type_var, value=t,
                              command=lambda: (self._show_type(), self._commit(), self._key_note()))
        self._popup(m, self.type_btn)

    def _menu(self):
        p = self.pal
        return tk.Menu(self.win, tearoff=0, bg=p["bg"], fg=p["text"], activebackground=p["accent_soft"],
                       activeforeground=p["text"], selectcolor=p["accent"], font=self.f, bd=0)

    def _popup(self, menu, widget):
        self._last_menu = menu
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    # ---- add / delete
    def _add_menu(self):
        m = self._menu()
        for preset in PRESETS:
            m.add_command(label=preset["name"], command=lambda pr=preset: self._add(pr))
        self._popup(m, self.add_btn)

    def _add(self, preset):
        self._commit()
        names = {it["name"] for it in self.items}
        name, n = preset["name"], 2
        while name in names:
            name, n = f"{preset['name']} {n}", n + 1
        self.items.append(dict(preset, name=name, _orig=None))
        self._refresh_list()
        self._load(len(self.items) - 1)
        self.status.config(text="Fill in the details, then press Test and Save.", fg=self.pal["muted"])

    def _delete(self):
        if self.cur is None:
            return
        if len(self.items) == 1:
            self.status.config(text="Keep at least one provider.", fg=self.pal["error"])
            return
        it = self.items.pop(self.cur)
        if it.get("_orig"):
            self.deleted.append(it["_orig"])
        self._refresh_list()
        self._load(min(self.cur, len(self.items) - 1))
        self.status.config(text=f"\"{it['name']}\" will be removed when you press Save.",
                           fg=self.pal["muted"])

    # ---- fetch models / test (network work runs in a thread)
    def _fetch(self):
        prov = self._form_provider()
        self.status.config(text="Fetching models…", fg=self.pal["muted"])

        def work():
            try:
                self.q.put(("models", list_models(prov)))
            except urllib.error.HTTPError as e:
                self.q.put(("error", _http_error_text(e)))
            except Exception as e:
                self.q.put(("error", f"Couldn't list models: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _test(self):
        prov = self._form_provider()
        self.status.config(text="Testing…", fg=self.pal["muted"])
        self.test_btn.set_enabled(False)

        def work():
            try:
                model = resolve_model(prov)
                msgs = [{"role": "user", "content": "Reply with just the word OK."}]
                out = "".join(c for k, c in stream_chat(msgs, model, threading.Event(), 3000, prov)
                              if k == "content")
                self.q.put(("ok", f"✓ Works. {model} replied: {visible_text(out).strip()[:60]}"))
            except urllib.error.HTTPError as e:
                self.q.put(("error", _http_error_text(e)))
            except urllib.error.URLError as e:
                self.q.put(("error", f"Couldn't connect: {e.reason}"))
            except Exception as e:
                self.q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if not self.win.winfo_exists():
            return
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "models":
                    if not val:
                        self.status.config(text="No models found.", fg=self.pal["error"])
                        continue
                    m = self._menu()
                    for mid in val[:60]:
                        m.add_command(label=mid, command=lambda v=mid: (
                            self.e_model.delete(0, "end"), self.e_model.insert(0, v)))
                    note = f"{len(val)} models found." + (" Showing the first 60; you can also type a name."
                                                           if len(val) > 60 else "")
                    self.status.config(text=note, fg=self.pal["muted"])
                    self._popup(m, self.fetch_btn)
                elif kind == "ok":
                    self.test_btn.set_enabled(True)
                    self.status.config(text=val, fg=self.pal["text"])
                else:
                    self.test_btn.set_enabled(True)
                    self.status.config(text=val, fg=self.pal["error"])
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    # ---- save
    def save(self):
        self._commit()
        names = [it["name"] for it in self.items]
        if any(not n for n in names):
            self.status.config(text="Every provider needs a name.", fg=self.pal["error"])
            return
        if len(set(names)) != len(names):
            self.status.config(text="Provider names must be different.", fg=self.pal["error"])
            return
        renames = {}
        try:
            for old in self.deleted:
                if old not in names:
                    key_delete(old)
            for it in self.items:
                old, new = it.get("_orig"), it["name"]
                if old and old != new:
                    renames[old] = new
                    k = key_get(old)
                    if k and not it.get("_forget"):
                        key_set(new, k)
                    key_delete(old)
                if it.get("_forget"):
                    key_delete(new)
                if it.get("_key"):
                    key_set(new, it["_key"])
        except OSError as e:
            self.status.config(text=f"Couldn't save the API key: {e}", fg=self.pal["error"])
            return
        clean = [{k: v for k, v in it.items() if not k.startswith("_") and v != ""} | {"name": it["name"]}
                 for it in self.items]
        for c in clean:
            c.setdefault("base_url", "")
            c.setdefault("model", "")
        save_setting("providers", clean)
        current = renames.get(CFG.get("provider"), CFG.get("provider"))
        if current not in names:
            current = names[0]
        save_setting("provider", current)
        for it in self.items:  # the form now reflects what's saved
            it["_orig"] = it["name"]
            it.pop("_key", None)
            it.pop("_forget", None)
        self.deleted = []
        self.e_key.delete(0, "end")
        self._key_note()
        self.status.config(text="Saved.", fg=self.pal["text"])
        if self.on_save:
            self.on_save(renames)

    def close(self):
        SettingsDialog._open = None
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
        self.window = None  # created on first lookup, then reused (tabs)

        threading.Thread(target=self._trigger_worker, daemon=True).start()
        self._start_hotkey()
        if CFG.get("ctrl_right_click", True):
            self._start_mouse_hook()
        self._start_tray()
        if any(p.get("type") == "anthropic" for p in providers()):
            # the Anthropic SDK takes ~2 s to import; load it now, not on the first lookup
            threading.Thread(target=lambda: __import__("anthropic"), daemon=True).start()
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
            pystray.MenuItem("Show window", lambda: self.ui_q.put(("show",)), default=True),
            pystray.MenuItem("Explain clipboard text", lambda: self.trigger_q.put("clipboard")),
            pystray.MenuItem(f"Trigger: Ctrl+Right-click or {hotkey_label(CFG.get('hotkey'))}", None, enabled=False),
            pystray.MenuItem("Settings…", lambda: self.ui_q.put(("settings",))),
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
                if item[0] == "settings":
                    SettingsDialog(self.root, on_save=self.window.on_settings_saved
                                   if self.window else None)
                    continue
                if item[0] == "show":
                    if not (self.window and self.window.show_existing()):
                        left, top, right, bottom = work_area_at(*cursor_pos())
                        self._toast("No lookups yet. Highlight text, then Ctrl + Right-click.",
                                    right - 20, bottom - 20, ms=3000, anchor_br=True)
                    continue
                if item[0] == "toast":
                    left, top, right, bottom = work_area_at(*cursor_pos())
                    self._toast(item[1], right - 20, bottom - 20, ms=3500, anchor_br=True)
                    continue
                _, text, x, y = item
                if not text:
                    self._toast("No text was selected (or the program blocked copying).", x, y)
                else:
                    if self.window is None:
                        self.window = ExplainWindow(self.root)
                    self.window.add_tab(text, x, y)
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


def selftest(out_path):
    """`LLM Explain.exe --selftest <file>`: check the packaged modules load, then exit."""
    lines = []
    for mod in ("anthropic", "pynput.keyboard", "pynput.mouse", "pystray", "PIL", "pyperclip"):
        try:
            m = __import__(mod)
            lines.append(f"ok   {mod} {getattr(m, '__version__', '')}")
        except Exception as e:
            lines.append(f"FAIL {mod}: {e}")
    lines.append(f"icon {'ok' if os.path.exists(ICON_ICO) else 'MISSING'}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI: coords match
    except Exception:
        user32.SetProcessDPIAware()
    single_instance_or_exit()
    App().run()


if __name__ == "__main__":
    main()
