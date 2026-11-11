# LLM Explain

<img src="icon.png" width="96" align="right">

Highlight text in any Windows or macOS program, then **Ctrl + Right-click** (or press **Ctrl+Alt+E**;
on a Mac **Control+Option+E**).
The explanation opens in a window near the mouse. Each lookup gets its own tab in the same
window, and you can ask follow-up questions in any tab.

It works with a local model in LM Studio out of the box, and with any API you add in Settings:
Claude (Anthropic), OpenAI, Google Gemini, OpenRouter, Groq, Mistral, DeepSeek, Ollama, or any
other OpenAI-compatible server.

## Setup (Windows)

For macOS, see [Setup (macOS)](#setup-macos).

1. For local models: in LM Studio, open the **Developer** tab and start the server
   (default `http://localhost:1234`). You don't need to load a model first. The tool uses the
   loaded model if there is one; otherwise LM Studio loads `fallback_model` on the first request.
   For cloud APIs, see [Model providers](#model-providers) below.
2. Install Python 3.10+, then build and install the program (one time):
   ```
   build_exe.bat
   powershell -ExecutionPolicy Bypass -File install.ps1 -Startup
   ```
   This puts `LLM Explain.exe` in `%LOCALAPPDATA%\Programs\LLM Explain`, creates an
   **LLM Explain** icon on your desktop, and starts it when you log in. Leave out `-Startup`
   if you don't want it to start at login (delete `LLM Explain` from `shell:startup` to undo).
3. Double-click the desktop icon. A notice confirms it's running, and its icon appears in the system tray.

The installed program reads `config.json` from its own folder (`%LOCALAPPDATA%\Programs\LLM Explain`).
Use **tray icon → Open config.json** to edit it.

**Run from source instead:** `python -m pip install -r requirements.txt`, then double-click
`start_llm_explain.bat` (and optionally `add_to_startup.bat`).

## Setup (macOS)

1. For local models, start LM Studio's server as in step 1 above. For cloud APIs, see
   [Model providers](#model-providers).
2. Install Python 3.10+ with Tk (the [python.org](https://www.python.org/downloads/macos/)
   installer includes it; with Homebrew, `brew install python-tk`). Then build and install the app:
   ```
   ./build_mac.sh
   ./install_mac.sh --startup
   ```
   This puts **LLM Explain.app** in `/Applications` (or `~/Applications`), starts it, and starts
   it when you log in. Leave out `--startup` if you don't want that
   (`./install_mac.sh --no-startup` undoes it).
3. The first time, macOS asks to allow **Accessibility** access. Turn on LLM Explain in
   **System Settings → Privacy & Security → Accessibility**. The tool needs it to see the
   shortcut and to send ⌘C; it starts working as soon as you allow it, no restart needed.
   An icon appears in the menu bar (the app has no Dock icon).

The app reads `config.json` from `~/Library/Application Support/LLM Explain`.
Use **menu bar icon → Open config.json** to edit it.

**Run from source instead:** double-click `start_llm_explain.command` (it sets up `.venv` the
first time). Accessibility access then goes to the app that runs it (Terminal, or Python).

On a Mac the window shortcuts use ⌘ instead of Ctrl (⌘W closes a tab, ⌘1/2/3 set the length).
Control + right-click means holding Control while you right-click (two-finger click on a trackpad);
Control + a normal click still opens the usual context menu. API keys go to the **Keychain**
(service "LLM Explain"). The build signs the app so that macOS keeps the Accessibility
permission across rebuilds (it matches the bundle identifier). If you installed a build from
before that change, remove LLM Explain from the Accessibility list with **−** and add it again
once.

## Model providers

Click the provider name at the top right of the window (or **tray icon → Settings…**):

- The menu switches the current tab to another provider. The tab answers again, and new lookups
  use that provider too.
- **Settings…** opens the provider list. **+ Add** offers presets (Anthropic, OpenAI, Gemini,
  OpenRouter, Groq, Mistral, DeepSeek, Ollama, custom). Enter your API key, press **Fetch** to pick
  a model, then **Test** and **Save**.

| Type | Use for | Base URL |
|---|---|---|
| LM Studio | local LM Studio server; model auto-detected; supports **Unload model** | `http://localhost:1234` |
| OpenAI-compatible | OpenAI, Gemini, OpenRouter, Groq, Mistral, DeepSeek, Ollama, vLLM, … | includes the version, e.g. `https://api.openai.com/v1` |
| Anthropic (Claude) | Claude, through the official `anthropic` Python SDK | leave empty |

API keys are stored in **Windows Credential Manager** (under "LLM Explain/&lt;provider name&gt;")
or the macOS **Keychain** (service "LLM Explain"), never in `config.json`. Optional per-provider settings in `config.json`: `"api_key_env"` reads
the key from an environment variable instead, and `"effort"` (`low`, `medium`, `high`) sets
Claude's effort level.

## Use

| Action | What it does |
|---|---|
| Ctrl + Right-click | Copy the highlighted text and explain it (the program's own right-click menu is suppressed) |
| Ctrl+Alt+E | Same, from the keyboard |
| Tray / menu bar icon → Explain clipboard text | Explain whatever is already on the clipboard |
| Click a tab / Ctrl+Tab | Switch between lookups (● marks a tab still answering) |
| ✕ on a tab / middle-click / Ctrl+W (⌘W) | Close that tab |
| Brief / Standard / Detailed | Change the current tab's answer length; it regenerates and is remembered (also Ctrl+1/2/3, on a Mac ⌘1/2/3) |
| Type + Enter | Ask a follow-up in the current tab |
| Regenerate / Copy | Ask again / copy the last answer |
| Provider name (top right) | Switch provider for this tab, or open Settings |
| Unload model (or tray → Unload model from LM Studio) | Unload the model from LM Studio to free memory; it reloads on the next question (LM Studio only) |
| Esc or the window's ✕ | Hide the window. Your tabs are kept; tray / menu bar → **Show window** brings it back |

Plain right-click (without Ctrl) works normally everywhere.

## Configure (`config.json`, restart the tool after editing)

- `providers` / `provider`: the provider list and the default one (easier to edit in Settings).
- `fallback_model`: the model to load when none is loaded. `google/gemma-4-e4b` is fast.
- `hotkey`: pynput format, e.g. `<ctrl>+<shift>+<space>`. Set `""` to disable. On a Mac, `<alt>`
  is Option and `<cmd>` is Command; the key is matched by its position on a US keyboard.
- `ctrl_right_click`: `false` to use only the hotkey.
- `length`: `brief`, `standard` or `detailed` (the popup switch also saves this).
- `theme`: `auto` (follow the system's light/dark setting), `light` or `dark`.
- `auto_unload_minutes`: unload the model automatically after this many idle minutes (only for models this tool loads). `0` = LM Studio's default.
- `max_tabs`: the most tabs kept open (default 8); the oldest finished tab closes first.
- `font_size`: text size (default 11). On a Mac 3 is added, so it looks the same size as on Windows.
- `always_on_top`: `false` lets other windows cover the explanation window.
- `drop_back`: when you click outside the window it drops behind the other windows (and comes back
  on top when you click it or look something up). `false` keeps it on top until you close it.
- `reasoning_allowance`: extra tokens for models that "think" before answering (e.g. Gemma 4). Raise it if answers come back empty.
- `restore_clipboard`: put your previous clipboard text back after grabbing the selection.
- `system_prompt`: change the style of the explanations (e.g. "answer in Spanish", "assume a PhD-level reader").

## Limits

- The tool copies the selection by sending Ctrl+C (⌘C on a Mac), so it works where that copies text.
  Windows terminals where Ctrl+C means "interrupt" won't work. Use "Explain clipboard text" there.
- Windows: programs running as Administrator don't accept input from a non-admin program.
  Run the tool as admin if you need it there.
- Windows: if the clipboard held an image before you triggered the tool, the image is replaced by
  the copied text. The Mac version puts back whatever was on the clipboard.
- Mac: while a password field has focus, macOS hides key presses from other apps, so the hotkey does nothing there.
