# Installs "LLM Explain.exe" for the current user and creates a desktop shortcut.
#   powershell -ExecutionPolicy Bypass -File install.ps1            (desktop shortcut)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Startup   (also start at login)
param([switch]$Startup)
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'dist\LLM Explain.exe'
if (-not (Test-Path $src)) { throw "Build it first: run build_exe.bat (missing $src)" }

$dest = Join-Path $env:LOCALAPPDATA 'Programs\LLM Explain'
New-Item -ItemType Directory -Force $dest | Out-Null

# Stop a running copy so the exe can be replaced
Get-Process -Name 'LLM Explain' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
Copy-Item $src $dest -Force

# Keep an existing installed config; otherwise copy the one next to this script
$cfg = Join-Path $dest 'config.json'
if (-not (Test-Path $cfg) -and (Test-Path (Join-Path $PSScriptRoot 'config.json'))) {
    Copy-Item (Join-Path $PSScriptRoot 'config.json') $cfg
}

$exe = Join-Path $dest 'LLM Explain.exe'
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $exe
    $s.WorkingDirectory = $dest
    $s.IconLocation = "$exe,0"
    $s.Description = 'Explain highlighted text with a local LM Studio model'
    $s.Save()
    "Shortcut: $path"
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'LLM Explain.lnk')
if ($Startup) { New-Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'LLM Explain.lnk') }
"Installed to $dest"
