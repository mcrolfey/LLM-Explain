@echo off
rem Starts LLM Explain in the background (no console window). Quit it from the tray icon.
rem Uses pythonw.exe from PATH; replace with a full path if Python isn't on PATH.
start "" pythonw "%~dp0llm_explain.py"
