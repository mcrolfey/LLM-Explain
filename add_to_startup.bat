@echo off
rem Creates a shortcut in your Startup folder so LLM Explain runs when you log in.
powershell -NoProfile -Command ^
  "$py=(Get-Command pythonw -ErrorAction Stop).Source;" ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\LLM Explain.lnk');" ^
  "$s.TargetPath=$py;" ^
  "$s.Arguments='\"%~dp0llm_explain.py\"';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.Save(); 'Added to Startup using ' + $py"
pause
