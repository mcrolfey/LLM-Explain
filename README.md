# LLM Explain

Highlight text in any Windows program, then **Ctrl + Right-click** (or press **Ctrl+Alt+E**).
A popup near the mouse explains the text using a local model in LM Studio. You can type
follow-up questions in the popup.

## Setup

1. In LM Studio, open the **Developer** tab and start the server (default `http://localhost:1234`).
   You don't need to load a model first. The tool uses the loaded model if there is one;
   otherwise LM Studio loads `fallback_model` on the first request.
2. Install Python 3.10+ and the dependencies:
   `python -m pip install -r requirements.txt`
3. Double-click `start_llm_explain.bat`. A blue **?** icon appears in the system tray.
4. Optional: run `add_to_startup.bat` once so the tool starts when you log in.

## Use

| Action | What it does |
|---|---|
| Ctrl + Right-click | Copy the highlighted text and explain it (the program's own right-click menu is suppressed) |
| Ctrl+Alt+E | Same, from the keyboard |
| Tray icon → Explain clipboard text | Explain whatever is already on the clipboard |
| In popup: type + Enter | Ask a follow-up |
| Esc | Close the popup |

Plain right-click (without Ctrl) works normally everywhere.

## Configure (`config.json`, restart the tool after editing)

- `model`: a fixed LM Studio model id (e.g. `google/gemma-4-26b-a4b`). Leave `""` for auto.
- `fallback_model`: the model to load when none is loaded. `google/gemma-4-e4b` is fast.
- `hotkey`: pynput format, e.g. `<ctrl>+<shift>+<space>`. Set `""` to disable.
- `ctrl_right_click`: `false` to use only the hotkey.
- `restore_clipboard`: put your previous clipboard text back after grabbing the selection.
- `system_prompt`: change the style of the explanations (e.g. "answer in Spanish", "assume a PhD-level reader").

## Limits

- The tool copies the selection by sending Ctrl+C, so it works where Ctrl+C copies text.
  Terminals where Ctrl+C means "interrupt" won't work. Use "Explain clipboard text" there.
- Programs running as Administrator don't accept input from a non-admin program.
  Run the tool as admin if you need it there.
- If the clipboard held an image before you triggered the tool, the image is replaced by the copied text.
