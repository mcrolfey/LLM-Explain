@echo off
rem Builds "dist\LLM Explain.exe" (a single file that runs without Python installed).
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :fail
if not exist icon.ico python make_icon.py || goto :fail
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "LLM Explain" --icon icon.ico --add-data "icon.ico;." ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  --hidden-import pystray._win32 --hidden-import anthropic ^
  llm_explain.py || goto :fail
echo.
echo Built: %~dp0dist\LLM Explain.exe
exit /b 0
:fail
echo Build failed.
exit /b 1
