"""
LLM Explain: highlight text in any Windows or macOS program, then press Ctrl + Right-click
(or the hotkey, default Ctrl+Alt+E, on a Mac Control+Option+E) to get an explanation from a
local LM Studio model or any model API you add in Settings.

How it works
  1. A global hook watches for Ctrl + Right-click and swallows that click, so the program
     under the mouse does not open its own context menu. (Windows: low-level mouse hook;
     macOS: Quartz event tap, which needs the Accessibility permission.)
  2. The tool sends Ctrl+C (Cmd+C on a Mac) to the foreground program to copy the highlighted
     text, reads it from the clipboard, then puts your previous clipboard back.
  3. The text goes to the model's streaming chat API and the answer streams into a small
     window near the mouse. You can ask follow-ups.

Platform-specific code lives in win_support.py and mac_support.py.
Windows: run with pythonw.exe (no console) via start_llm_explain.bat. Quit from the tray icon.
macOS: run start_llm_explain.command, or build the app with build_mac.sh. Quit from the menu bar.
"""

import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import font as tkfont

IS_MAC = sys.platform == "darwin"
LOG = logging.getLogger("llm_explain")
if IS_MAC:
    import mac_support as plat
else:
    import win_support as plat

FROZEN = getattr(sys, "frozen", False)  # running as a PyInstaller exe / .app
# config.json lives next to the .exe (or next to this script when run with Python).
# The Mac app keeps it in ~/Library/Application Support/LLM Explain (the .app is read-only).
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
# bundled read-only files (icon) live in the PyInstaller temp dir when frozen
RES = getattr(sys, "_MEIPASS", HERE)
CONFIG_DIR = plat.SUPPORT_DIR if (IS_MAC and FROZEN) else HERE
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
# Windows: .ico for the tray and windows. macOS: monochrome menu bar template image.
ICON_PATH = os.path.join(RES, "icon_menubar.png" if IS_MAC else "icon.ico")

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
    "drop_back": True,
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


def load_config():
    global CONFIG_ERROR
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        try:  # first run: write a config the user can edit
            os.makedirs(CONFIG_DIR, exist_ok=True)
            bundled = os.path.join(RES, "config.json")
            if os.path.abspath(bundled) != os.path.abspath(CONFIG_PATH) and os.path.exists(bundled):
                shutil.copyfile(bundled, CONFIG_PATH)  # Mac app: start from the shipped config
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            else:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_CONFIG, f, indent=2)
        except (OSError, json.JSONDecodeError):
            pass
    except json.JSONDecodeError as e:
        CONFIG_ERROR = f"config.json has an error and was ignored (defaults used):\n{e}"
    # "localhost" makes Windows try IPv6 (::1) first; LM Studio listens on IPv4, so every
    # request would wait ~2 s for the fallback. Go straight to 127.0.0.1.
    cfg["base_url"] = cfg["base_url"].rstrip("/").replace("://localhost", "://127.0.0.1")
    return cfg


CFG = load_config()

# ------------------------------------------------------------------ providers
# A provider is a dict: {"name", "type", "base_url", "model", optional "api_key_env", "effort"}
#   type "lmstudio":  LM Studio (root URL, e.g. http://localhost:1234). Auto model, unload, TTL.
#   type "openai":    any OpenAI-compatible API (base URL incl. version, e.g. .../v1).
#   type "anthropic": Claude through the official Anthropic SDK.
# API keys are kept in Windows Credential Manager / the macOS Keychain, never in config.json.
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


def api_key(prov):
    """Saved key, else the environment variable named in "api_key_env" (if any)."""
    if prov.get("_key"):  # typed in Settings but not saved yet (Test / Fetch)
        return prov["_key"]
    k = plat.key_get(prov.get("name", ""))
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
        obj = json.loads(body)
        if isinstance(obj, list) and obj:  # Gemini wraps the error in a list
            obj = obj[0]
        err = obj.get("error", body)
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except Exception:
        pass
    hint = {401: "Check the API key in Settings.", 403: "The API key isn't allowed to do this.",
            404: "Check the base URL and model name in Settings.",
            429: "Rate limit or quota reached. Wait a moment or check your account."}.get(e.code, "")
    return f"HTTP {e.code}: {msg.strip()[:300]}" + (f"\n{hint}" if hint else "")


# Model ids that can't answer a chat in text (speech, images, video, embeddings, live audio...).
# Providers list them alongside chat models; picking one makes every request fail.
_NON_CHAT = re.compile(
    r"(^|[-_/.])(tts|embed\w*|embedding|imagen|veo|image|images|aqa|live|native-audio|audio|"
    r"transcribe|whisper|dall-e|moderation|realtime|sora|lyria|computer-use|robotics)([-_/.]|$)"
    r"|^(babbage|davinci)-|^tts-|^gpt-image", re.I)


def is_chat_model(model_id):
    return bool(model_id) and not _NON_CHAT.search(model_id.replace("models/", ""))


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
    return sorted(i.replace("models/", "") for i in ids if is_chat_model(i))


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
            if e.code in (400, 404) and not is_chat_model(model):
                text += (f"\n\"{model}\" is a speech/image/embedding model, which can't answer "
                         "in text. Open Settings, press Fetch and choose a chat model.")
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
        lms = plat.lms_cli()
        if not lms:
            return "This LM Studio version can't unload remotely. Update LM Studio."
        import subprocess
        subprocess.run([lms, "unload", "--all"], capture_output=True, timeout=60,
                       **plat.SUBPROCESS_FLAGS)  # no console window on Windows
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


def current_theme():
    """Returns (palette, is_dark). config "theme": "auto" | "light" | "dark"."""
    name = CFG.get("theme", "auto")
    if name not in THEMES:
        name = "dark" if plat.system_dark_mode() else "light"
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


_FONT_FAMILIES = None


def pick_font(root, *names):
    global _FONT_FAMILIES
    if _FONT_FAMILIES is None:
        _FONT_FAMILIES = set(tkfont.families(root))
    return next((n for n in names if n in _FONT_FAMILIES), names[-1])


MOD = "Command" if IS_MAC else "Control"  # shortcut modifier in Tk bindings (Cmd+W / Ctrl+W)
MOD_STATE = 0x8 if IS_MAC else 0x4        # the same modifier in event.state
# Tk 8.6 on macOS numbers the middle button 3 (and the right button 2)
MIDDLE_BUTTON = "<Button-3>" if IS_MAC and tk.TkVersion < 8.7 else "<Button-2>"


def ui_scale(widget):
    """Pixels per Windows-96-dpi pixel. Tk on macOS already works in points: use 1:1."""
    return 1.0 if IS_MAC else widget.winfo_fpixels("1i") / 96.0


def ui_fonts(root):
    """(text, display, mono) families and a size offset. Tk on macOS takes sizes as 72-dpi
    points, so the same number looks ~25% smaller than on Windows; the offset evens it out."""
    if IS_MAC:
        system = tkfont.nametofont("TkDefaultFont").actual()["family"]  # San Francisco
        return system, system, pick_font(root, "SF Mono", "Menlo", "Courier"), 3
    return (pick_font(root, "Segoe UI Variable Text", "Segoe UI", "TkDefaultFont"),
            pick_font(root, "Segoe UI Variable Display", "Segoe UI", "TkDefaultFont"),
            pick_font(root, "Cascadia Mono", "Consolas", "Courier New"), 0)


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
        self.on_top = bool(CFG.get("always_on_top", True))
        self._topmost = None
        self._focus_job = None
        self.set_on_top(self.on_top)
        # Click outside the window -> it drops behind; click it or look something up -> back on top
        w.bind("<FocusIn>", lambda e: self._focus_changed(), add="+")
        w.bind("<FocusOut>", lambda e: self._focus_changed(), add="+")
        w.protocol("WM_DELETE_WINDOW", self.hide)
        w.bind("<Escape>", lambda e: self.hide())
        w.bind(f"<{MOD}-w>", lambda e: self.active and self.close_tab(self.active))
        w.bind(f"<{MOD}-W>", lambda e: self.active and self.close_tab(self.active))
        w.bind("<Control-Tab>", lambda e: (self.cycle(1), "break")[1])
        w.bind("<Control-Shift-Tab>", lambda e: (self.cycle(-1), "break")[1])
        for i, k in enumerate(LENGTH_ORDER, 1):
            w.bind(f"<{MOD}-Key-{i}>", lambda e, k=k: self.set_length(k))

        scale = ui_scale(w)
        px = self.px = lambda v: max(1, int(round(v * scale)))

        ui, ui_disp, mono, offset = ui_fonts(root)
        fs = CFG["font_size"] + offset
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
        t.bind("<Key>", lambda e: None if (e.state & MOD_STATE and e.keysym.lower() in ("c", "a")) else "break")
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
            wdg.bind(MIDDLE_BUTTON, lambda e, t=tab: self.close_tab(t))  # middle-click
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
        left, top, right, bottom = plat.work_area_at(x, y)
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
            plat.style_titlebar(w, self.pal, self.dark)
            self.styled = True
        plat.activate_app()  # macOS: take focus from the app the text was selected in
        self.set_on_top(self.on_top)
        w.lift()
        w.focus_force()
        self.entry.focus_set()

    def set_on_top(self, on):
        if on != self._topmost:
            self.win.attributes("-topmost", bool(on))
            self._topmost = on

    def _focus_changed(self):
        # FocusOut also fires when focus just moves between widgets; check once it settles
        if self._focus_job:
            self.win.after_cancel(self._focus_job)
        self._focus_job = self.win.after(150, self._check_focus)

    def _check_focus(self):
        self._focus_job = None
        if not CFG.get("drop_back", True) or self.win.state() != "normal":
            return  # (lowering a hidden window would show it again on macOS)
        try:
            inside = self.win.focus_get() is not None  # None: another app has focus
        except (KeyError, tk.TclError):
            inside = True  # focus is in one of our popup menus
        if inside:
            self.set_on_top(self.on_top)
        else:
            self.set_on_top(False)
            self.win.lower()  # behind the window that was clicked

    def show_existing(self):
        if self.tabs:
            self.bring_to_front()
            return True
        return False

    def hide(self):
        self.win.withdraw()
        if not SettingsDialog.is_open():
            plat.hide_app()  # macOS: hand focus back to the previous app

    def poll(self):
        for t in list(self.tabs):
            t.poll_once()
        self.win.after(50, self.poll)


class SettingsDialog:
    """Add, edit, test and delete model providers. API keys go to the OS keystore."""
    _open = None

    @classmethod
    def is_open(cls):
        return bool(cls._open and cls._open.win.winfo_exists())

    TYPE_HINTS = {
        "lmstudio": "Local LM Studio server (root URL, no /v1). Leave the model empty to use "
                    "whatever is loaded. No API key needed.",
        "openai": "Any OpenAI-compatible API. The base URL includes the version, e.g. "
                  "https://api.openai.com/v1. Press Fetch to pick a model.",
        "anthropic": "Claude, through Anthropic's official SDK. Get an API key at "
                     "console.anthropic.com. Leave the base URL empty.",
    }

    def __init__(self, root, on_save=None):
        if SettingsDialog.is_open():
            plat.activate_app()
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
        scale = ui_scale(w)
        px = self.px = lambda v: max(1, int(round(v * scale)))
        ui, ui_disp, _mono, offset = ui_fonts(root)
        fs = CFG["font_size"] + offset
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
        left_, top_, right_, bottom_ = plat.work_area_at(*plat.cursor_pos())
        # a fixed size stops the dialog jumping wider when another provider is selected
        w.geometry(f"{sw}x{sh}+{left_ + (right_ - left_ - sw) // 2}+{top_ + (bottom_ - top_ - sh) // 3}")
        w.deiconify()
        plat.style_titlebar(w, p, dark)
        plat.activate_app()
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
            it["_key"] = plat.key_get(it.get("_orig") or it["name"])
        return it

    def _key_note(self):
        it = self.items[self.cur]
        saved = bool(plat.key_get(it.get("_orig") or "")) and not it.get("_forget")
        if it.get("type") == "lmstudio":
            text = "Usually not needed for LM Studio."
        elif saved:
            text = f"A key is saved in {plat.KEYSTORE}. Leave blank to keep it."
        else:
            text = f"Stored in {plat.KEYSTORE}, not in config.json."
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
                    plat.key_delete(old)
            for it in self.items:
                old, new = it.get("_orig"), it["name"]
                if old and old != new:
                    renames[old] = new
                    k = plat.key_get(old)
                    if k and not it.get("_forget"):
                        plat.key_set(new, k)
                    plat.key_delete(old)
                if it.get("_forget"):
                    plat.key_delete(new)
                if it.get("_key"):
                    plat.key_set(new, it["_key"])
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
        plat.setup_tk(self.root, ICON_PATH)
        if IS_MAC:
            # Opening the app again from Finder/Launchpad shows the window; Cmd+Q quits cleanly
            self.root.createcommand("::tk::mac::ReopenApplication", lambda *a: self.ui_q.put(("show",)))
            self.root.createcommand("::tk::mac::Quit", lambda *a: self.ui_q.put(("quit",)))
        self.ui_q = queue.Queue()
        self.trigger_q = queue.Queue()
        self.tray = None
        self.window = None  # created on first lookup, then reused (tabs)

        threading.Thread(target=self._trigger_worker, daemon=True).start()
        self.triggers = plat.Triggers(CFG.get("hotkey"), CFG.get("ctrl_right_click", True),
                                      lambda: self.trigger_q.put("selection"))
        self.triggers_ok = self.triggers.start()
        LOG.info("start: shortcut %s, accessibility=%s, hotkey=%r, ctrl_right_click=%s",
                 "active" if self.triggers_ok else "NOT active (waiting for permission)",
                 self.triggers.has_permission(), CFG.get("hotkey"), CFG.get("ctrl_right_click", True))
        self._retries = 0
        if not self.triggers_ok:
            # macOS: no Accessibility permission yet. Ask once, then retry until it's granted.
            self.triggers.has_permission(prompt=True)
            self.root.after(2000, self._retry_triggers)
        self._start_tray()
        if any(p.get("type") == "anthropic" for p in providers()):
            # the Anthropic SDK takes ~2 s to import; load it now, not on the first lookup
            threading.Thread(target=lambda: __import__("anthropic"), daemon=True).start()
        self.root.after(50, self._poll)
        self.root.after(300, self._startup_notice)

    def _trigger_text(self):
        hk = CFG.get("hotkey")
        click = plat.TRIGGER_CLICK if CFG.get("ctrl_right_click", True) else ""
        keys = plat.hotkey_label(hk) if hk else ""
        return f"{click} (or {keys})" if click and keys else (click or keys or f"Explain clipboard text in the {plat.TRAY}")

    def _startup_notice(self):
        """The app lives in the tray / menu bar, so show briefly that launching worked."""
        msg = (f"LLM Explain is running.\nHighlight text, then {self._trigger_text()}."
               f"\nQuit from the {plat.TRAY}.")
        ms = 3500
        if not self.triggers_ok:
            msg += ("\n\nTo respond to the shortcut, LLM Explain needs Accessibility access:\n"
                    "System Settings → Privacy & Security → Accessibility → turn it on.\n"
                    "It starts working as soon as you do.")
            ms = 12000
        if CONFIG_ERROR:
            msg += "\n\n" + CONFIG_ERROR
            ms = max(ms, 6000)
        self._corner_toast(msg, ms)

    def _retry_triggers(self):
        self._retries += 1
        if self.triggers.start():
            LOG.info("shortcut active after permission was granted")
            self.triggers_ok = True
            self._corner_toast(f"Ready: highlight text, then {self._trigger_text()}.", 4000)
        else:
            self.root.after(2000, self._retry_triggers)

    def _trigger_worker(self):
        while True:
            source = self.trigger_q.get()
            # drop duplicate triggers that piled up
            while not self.trigger_q.empty():
                self.trigger_q.get_nowait()
            x, y = plat.cursor_pos()
            LOG.info("trigger: %s", source)
            if source == "clipboard":
                text = (plat.clipboard_text() or "").strip()
            else:
                time.sleep(0.05)
                text = plat.copy_selection(CFG.get("restore_clipboard", True))
            LOG.info("copied %d characters", len(text))
            self.ui_q.put(("open", text, x, y))

    def _tray_unload(self):
        threading.Thread(target=lambda: self.ui_q.put(("toast", unload_models_safe())),
                         daemon=True).start()

    def _start_tray(self):
        items = [
            ("Show window", lambda: self.ui_q.put(("show",)), True),
            ("Explain clipboard text", lambda: self.trigger_q.put("clipboard"), False),
            (f"Trigger: {self._trigger_text()}", None, False),
            ("Settings…", lambda: self.ui_q.put(("settings",)), False),
            ("Unload model from LM Studio", self._tray_unload, False),
            ("Open config.json", lambda: plat.open_file(CONFIG_PATH), False),
        ] + ([
            ("Open Accessibility settings…", plat.open_accessibility_settings, False),
            ("Open log", lambda: plat.open_file(plat.LOG_PATH), False),
        ] if IS_MAC else []) + [
            ("Quit", lambda: self.ui_q.put(("quit",)), False),
        ]
        try:
            self.tray = plat.Tray(ICON_PATH, items)
        except Exception as e:
            print(f"Could not create the {plat.TRAY}: {e}")

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
                        self._corner_toast(f"No lookups yet. Highlight text, then "
                                           f"{self._trigger_text()}.", 3000)
                    continue
                if item[0] == "toast":
                    self._corner_toast(item[1], 3500)
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

    def _corner_toast(self, msg, ms):
        """A notice in the screen corner next to the tray (Windows) or menu bar (macOS)."""
        left, top, right, bottom = plat.work_area_at(*plat.cursor_pos())
        if plat.NOTICE_CORNER == "tr":
            self._toast(msg, right - 20, top + 8, ms=ms, anchor="tr")
        else:
            self._toast(msg, right - 20, bottom - 20, ms=ms, anchor="br")

    def _toast(self, msg, x, y, ms=1800, anchor=None):
        plat.show_app_quietly()  # macOS: the app may be hidden after the window closed
        tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        pal, _ = current_theme()
        tw.config(bg=pal["border"])
        tk.Label(tw, text=msg, bg=pal["subtle"], fg=pal["text"], padx=14, pady=10,
                 justify="left").pack(padx=1, pady=1)
        if anchor:  # (x, y) is the bottom-right ("br") or top-right ("tr") corner
            tw.update_idletasks()
            x -= tw.winfo_reqwidth() + 12
            y = y - tw.winfo_reqheight() - 12 if anchor == "br" else y - 12
        tw.geometry(f"+{x + 12}+{y + 12}")
        tw.deiconify()
        tw.lift()
        tw.bind("<Button-1>", lambda e: tw.destroy())
        tw.after(ms, tw.destroy)

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.triggers.stop()
        self.root.destroy()
        os._exit(0)

    def run(self):
        self.root.mainloop()


def selftest(out_path):
    """`LLM Explain.exe --selftest <file>`: check the packaged modules load, then exit."""
    lines = []
    for mod in plat.SELFTEST_MODULES:
        try:
            m = __import__(mod)
            lines.append(f"ok   {mod} {getattr(m, '__version__', '')}")
        except Exception as e:
            lines.append(f"FAIL {mod}: {e}")
    try:
        r = tk.Tk()
        lines.append(f"ok   tk {r.tk.call('info', 'patchlevel')}")
        r.destroy()
    except Exception as e:
        lines.append(f"FAIL tk: {e}")
    lines.append(f"icon {'ok' if os.path.exists(ICON_PATH) else 'MISSING'}")
    lines.append(f"config {CONFIG_PATH}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def setup_logging():
    """macOS: ~/Library/Logs/LLM Explain.log (menu bar → Open log). Windows: no log file."""
    path = getattr(plat, "LOG_PATH", None)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=200_000, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(h)
        LOG.setLevel(logging.INFO)
    except OSError:
        pass


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        return
    setup_logging()
    plat.setup_process()
    plat.single_instance_or_exit()
    App().run()


if __name__ == "__main__":
    main()
