#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot installer for wisperflow on Windows 10/11.

.DESCRIPTION
    Installs Python + Ollama via winget, creates a virtualenv, installs
    dependencies, pulls the LLM model, seeds .env, creates a launcher and
    Start Menu shortcut. Idempotent - safe to re-run.

.PARAMETER Model
    Ollama model to pull. Default: qwen2.5:3b.

.PARAMETER Autostart
    Add wisperflow to Windows startup without prompting.

.PARAMETER SkipAutostart
    Skip autostart prompt and don't add to startup.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Autostart
    .\install.ps1 -Model llama3.2:3b
#>
[CmdletBinding()]
param(
    [string]$Model = "qwen2.5:3b",
    [switch]$Autostart,
    [switch]$SkipAutostart
)

$ErrorActionPreference = "Stop"

# --- helpers --------------------------------------------------------------

function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "    [skip] $msg" -ForegroundColor DarkGray }
function Write-Warn2($m)  { Write-Host "    [warn] $m" -ForegroundColor Yellow }
function Fail($msg)       { Write-Host ""; Write-Host "[error] $msg" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $u = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($m, $u) | Where-Object { $_ }) -join ";"
}

function Test-Command($name) {
    $null = Get-Command $name -ErrorAction SilentlyContinue
    return $?
}

function Get-PythonVersion($exe) {
    try {
        $v = & $exe --version 2>&1
        if ($v -match '(\d+)\.(\d+)\.(\d+)') {
            return [Version]("{0}.{1}.{2}" -f $matches[1], $matches[2], $matches[3])
        }
    } catch {}
    return $null
}

# --- preflight ------------------------------------------------------------

Write-Host "wisperflow installer" -ForegroundColor White
Write-Host "--------------------"

$RepoRoot = $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot "main.py"))) {
    Fail "install.ps1 must live in the wisperflow repo root (expected main.py next to it)."
}
Set-Location $RepoRoot

if ([Environment]::OSVersion.Version.Major -lt 10) {
    Fail "Windows 10 or later required."
}

if (-not (Test-Command "winget")) {
    Fail "winget not found. Install 'App Installer' from the Microsoft Store, then re-run."
}

Write-Ok "Windows $([Environment]::OSVersion.Version), PowerShell $($PSVersionTable.PSVersion), winget present"

# --- Python ---------------------------------------------------------------

Write-Step "Python 3.10+"

$pythonExe = $null
foreach ($cand in @("py -3.12", "py -3.11", "py -3.10", "python")) {
    $parts = $cand.Split(" ", 2)
    if (-not (Test-Command $parts[0])) { continue }
    $v = Get-PythonVersion $cand
    if ($v -and $v.Major -eq 3 -and $v.Minor -ge 10) {
        $pythonExe = $cand
        Write-Ok "found $cand ($v)"
        break
    }
}

if (-not $pythonExe) {
    Write-Host "    installing Python 3.12 via winget (this can take a minute)..."
    & winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "winget failed to install Python.Python.3.12 (exit $LASTEXITCODE)" }
    Refresh-Path
    foreach ($cand in @("py -3.12", "python")) {
        $parts = $cand.Split(" ", 2)
        if (-not (Test-Command $parts[0])) { continue }
        $v = Get-PythonVersion $cand
        if ($v -and $v.Major -eq 3 -and $v.Minor -ge 10) { $pythonExe = $cand; break }
    }
    if (-not $pythonExe) { Fail "Python install succeeded but no suitable python found on PATH. Restart the shell and re-run." }
    Write-Ok "installed $pythonExe"
}

# --- Ollama ---------------------------------------------------------------

Write-Step "Ollama"

if (Test-Command "ollama") {
    Write-Ok "already installed"
} else {
    Write-Host "    installing Ollama via winget..."
    & winget install --id Ollama.Ollama -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "winget failed to install Ollama.Ollama (exit $LASTEXITCODE)" }
    Refresh-Path
    if (-not (Test-Command "ollama")) {
        $guess = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
        if (Test-Path (Join-Path $guess "ollama.exe")) { $env:Path = "$guess;$env:Path" }
    }
    if (-not (Test-Command "ollama")) { Fail "Ollama install succeeded but 'ollama' not on PATH. Restart the shell and re-run." }
    Write-Ok "installed"
}

# --- venv + deps ----------------------------------------------------------

Write-Step "Python virtual environment"

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Skip ".venv already exists"
} else {
    Write-Host "    creating .venv with $pythonExe..."
    $parts = $pythonExe.Split(" ", 2)
    if ($parts.Count -eq 2) {
        & $parts[0] $parts[1] -m venv .venv
    } else {
        & $parts[0] -m venv .venv
    }
    if (-not (Test-Path $venvPython)) { Fail "venv creation failed - $venvPython not found" }
    Write-Ok "created .venv"
}

Write-Step "pip install -r requirements.txt (may take a minute)"
& $venvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed (exit $LASTEXITCODE)" }
& $venvPython -m pip install -r (Join-Path $RepoRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (exit $LASTEXITCODE)" }
Write-Ok "dependencies installed"

# --- Ollama server --------------------------------------------------------

Write-Step "Ollama server"

function Test-OllamaUp {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
        return $true
    } catch { return $false }
}

if (Test-OllamaUp) {
    Write-Ok "already running"
} else {
    Write-Host "    launching 'ollama serve' in background..."
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden | Out-Null
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-OllamaUp) { $ready = $true; break }
    }
    if (-not $ready) { Fail "Ollama didn't respond on 127.0.0.1:11434 within 30s" }
    Write-Ok "server up"
}

# --- model pull -----------------------------------------------------------

Write-Step "LLM model: $Model"

$list = & ollama list 2>$null
$modelPresent = $false
if ($LASTEXITCODE -eq 0 -and $list) {
    foreach ($line in $list) {
        if ($line -match [regex]::Escape($Model)) { $modelPresent = $true; break }
    }
}

if ($modelPresent) {
    Write-Ok "already pulled"
} else {
    Write-Host "    pulling $Model (~2GB, progress below)..."
    & ollama pull $Model
    if ($LASTEXITCODE -ne 0) { Fail "ollama pull $Model failed (exit $LASTEXITCODE)" }
    Write-Ok "pulled"
}

# --- .env -----------------------------------------------------------------

Write-Step ".env configuration"

$envPath = Join-Path $RepoRoot ".env"
if (Test-Path $envPath) {
    Write-Skip ".env already exists (not touching)"
} else {
    $envTemplate = @"
# wisperflow configuration

# Local Ollama for free offline LLM rewriting.
WISPERFLOW_REWRITER=ollama
WISPERFLOW_OLLAMA_MODEL=$Model
OLLAMA_HOST=http://127.0.0.1:11434

# Whisper STT. base.en is the fast CPU default; bump to small.en for accuracy.
WISPERFLOW_MODEL=base.en
WISPERFLOW_DEVICE=auto
WISPERFLOW_COMPUTE=auto
WISPERFLOW_LANG=en

# Push-to-talk hotkey. Hold to record, release to transcribe.
WISPERFLOW_HOTKEY=<ctrl>+<alt>+<space>

# Paste mode (clipboard + Ctrl+V) is faster than typing.
WISPERFLOW_INJECT=paste

# --- Microphone ---
# Run `wisperflow.cmd --list-devices` to see options. Accepts an index ("1")
# or a case-insensitive substring of the device name ("realtek", "oculus").
#WISPERFLOW_INPUT_DEVICE=
#WISPERFLOW_INPUT_GAIN=1.0

# --- Status indicator ---
#WISPERFLOW_INDICATOR=1
#WISPERFLOW_BEEP=1
"@
    Set-Content -Path $envPath -Value $envTemplate -Encoding UTF8
    Write-Ok "wrote .env (model=$Model)"
}

# --- launcher -------------------------------------------------------------

Write-Step "Launcher"

$launcher = Join-Path $RepoRoot "wisperflow.cmd"
$launcherBody = @"
@echo off
setlocal
pushd "%~dp0"
".venv\Scripts\python.exe" main.py %*
set RC=%ERRORLEVEL%
popd
exit /b %RC%
"@
Set-Content -Path $launcher -Value $launcherBody -Encoding ASCII
Write-Ok "wrote wisperflow.cmd"

# --- Start Menu shortcut --------------------------------------------------

Write-Step "Start Menu shortcut"

try {
    $wsh = New-Object -ComObject WScript.Shell
    $startMenu = [Environment]::GetFolderPath("Programs")
    $scPath = Join-Path $startMenu "Wisperflow.lnk"
    $sc = $wsh.CreateShortcut($scPath)
    $sc.TargetPath = $launcher
    $sc.WorkingDirectory = $RepoRoot
    $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,172"
    $sc.Description = "Push-to-talk dictation with local Whisper + LLM polish"
    $sc.Save()
    Write-Ok "created $scPath"
} catch {
    Write-Warn2 "failed to create Start Menu shortcut: $_"
}

# --- autostart ------------------------------------------------------------

Write-Step "Autostart"

$addAutostart = $false
if ($Autostart) {
    $addAutostart = $true
} elseif ($SkipAutostart) {
    Write-Skip "skipped (-SkipAutostart)"
} else {
    $resp = Read-Host "Add wisperflow to Windows startup? [y/N]"
    if ($resp -match '^(y|yes)$') { $addAutostart = $true }
}

if ($addAutostart) {
    try {
        $wsh = New-Object -ComObject WScript.Shell
        $startupDir = [Environment]::GetFolderPath("Startup")
        $asPath = Join-Path $startupDir "Wisperflow.lnk"
        $sc = $wsh.CreateShortcut($asPath)
        $sc.TargetPath = $launcher
        $sc.WorkingDirectory = $RepoRoot
        $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,172"
        $sc.WindowStyle = 7  # minimized
        $sc.Save()
        Write-Ok "added to startup ($asPath)"
    } catch {
        Write-Warn2 "failed to add autostart: $_"
    }
}

# --- done -----------------------------------------------------------------

Write-Host ""
Write-Host "done." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Pick your microphone:"
Write-Host "       .\wisperflow.cmd --list-devices"
Write-Host "     then edit .env and set e.g.  WISPERFLOW_INPUT_DEVICE=HyperX"
Write-Host ""
Write-Host "  2. Test the mic (talk into it, watch the level bar):"
Write-Host "       .\wisperflow.cmd --mic-test 5"
Write-Host ""
Write-Host "  3. Launch wisperflow (or use the Start Menu shortcut):"
Write-Host "       .\wisperflow.cmd"
Write-Host "     Hold Ctrl+Alt+Space, speak, release. Ctrl+C to quit."
Write-Host ""
