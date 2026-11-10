# LLM Explain

<img src="icon.png" width="96" align="right">

Highlight text in any Windows program, then **Ctrl + Right-click** (or press **Ctrl+Alt+E**).
The explanation opens in a window near the mouse. Each lookup gets its own tab in the same
window, and you can ask follow-up questions in any tab.

It works with a local model in LM Studio out of the box, and with any API you add in Settings:
Claude (Anthropic), OpenAI, Google Gemini, OpenRouter, Groq, Mistral, DeepSeek, Ollama, or any
other OpenAI-compatible server.

## Setup

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

API keys are stored in **Windows Credential Manager** (under "LLM Explain/&lt;provider name&gt;"),
never in `config.json`. Optional per-provider settings in `config.json`: `"api_key_env"` reads
the key from an environment variable instead, and `"effort"` (`low`, `medium`, `high`) sets
Claude's effort level.

## Use

| Action | What it does |
|---|---|
| Ctrl + Right-click | Copy the highlighted text and explain it (the program's own right-click menu is suppressed) |
| Ctrl+Alt+E | Same, from the keyboard |
| Tray icon → Explain clipboard text | Explain whatever is already on the clipboard |
| Click a tab / Ctrl+Tab | Switch between lookups (● marks a tab still answering) |
| ✕ on a tab / middle-click / Ctrl+W | Close that tab |
| Brief / Standard / Detailed | Change the current tab's answer length; it regenerates and is remembered (also Ctrl+1/2/3) |
| Type + Enter | Ask a follow-up in the current tab |
| Regenerate / Copy | Ask again / copy the last answer |
| Provider name (top right) | Switch provider for this tab, or open Settings |
| Unload model (or tray → Unload model from LM Studio) | Unload the model from LM Studio to free memory; it reloads on the next question (LM Studio only) |
| Esc or the window's ✕ | Hide the window. Your tabs are kept; tray → **Show window** brings it back |

Plain right-click (without Ctrl) works normally everywhere.

## Configure (`config.json`, restart the tool after editing)

- `providers` / `provider`: the provider list and the default one (easier to edit in Settings).
- `fallback_model`: the model to load when none is loaded. `google/gemma-4-e4b` is fast.
- `hotkey`: pynput format, e.g. `<ctrl>+<shift>+<space>`. Set `""` to disable.
- `ctrl_right_click`: `false` to use only the hotkey.
- `length`: `brief`, `standard` or `detailed` (the popup switch also saves this).
- `theme`: `auto` (follow Windows light/dark), `light` or `dark`.
- `auto_unload_minutes`: unload the model automatically after this many idle minutes (only for models this tool loads). `0` = LM Studio's default.
- `max_tabs`: the most tabs kept open (default 8); the oldest finished tab closes first.
- `always_on_top`: `false` lets other windows cover the explanation window.
- `reasoning_allowance`: extra tokens for models that "think" before answering (e.g. Gemma 4). Raise it if answers come back empty.
- `restore_clipboard`: put your previous clipboard text back after grabbing the selection.
- `system_prompt`: change the style of the explanations (e.g. "answer in Spanish", "assume a PhD-level reader").

## Limits

- The tool copies the selection by sending Ctrl+C, so it works where Ctrl+C copies text.
  Terminals where Ctrl+C means "interrupt" won't work. Use "Explain clipboard text" there.
- Programs running as Administrator don't accept input from a non-admin program.
  Run the tool as admin if you need it there.
- If the clipboard held an image before you triggered the tool, the image is replaced by the copied text.
