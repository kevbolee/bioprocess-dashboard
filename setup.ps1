#Requires -Version 5.1

# Bioreactor OPC Dashboard - Prerequisites Installer
# Installs: Python 3 (Miniforge3), ODBC Driver 17 for SQL Server,
#           Python packages, and creates .env from .env.example.
# Run via setup.bat - it handles the admin prompt automatically.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step { param($msg) Write-Host "" ; Write-Host ">>> $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg) Write-Host "    [ OK ]  $msg" -ForegroundColor Green }
function Write-Info { param($msg) Write-Host "    [ -- ]  $msg" -ForegroundColor Gray }
function Write-Warn { param($msg) Write-Host "    [ !! ]  $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "    [ XX ]  $msg" -ForegroundColor Red }

# --- Self-elevate to Administrator ---

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal   = New-Object Security.Principal.WindowsPrincipal($currentUser)
$isAdmin     = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Relaunching as Administrator (required for ODBC driver install)..."
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    Start-Process powershell.exe -ArgumentList $argList -Verb RunAs
    exit
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TempDir   = Join-Path $env:TEMP "bioreactor-setup"
if (-not (Test-Path $TempDir)) { New-Item -ItemType Directory -Path $TempDir | Out-Null }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Bioreactor OPC Dashboard - Prerequisites Installer"            -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan


# ================================================================
# 1. PYTHON
# ================================================================

Write-Step "1/4  Checking Python..."

function Test-PythonExe {
    param($path)
    if (-not (Test-Path $path)) { return $false }
    try {
        & $path --version 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$PythonExe  = $null
$candidates = @(
    "C:\ProgramData\mambaforge\python.exe",
    "C:\ProgramData\miniforge3\python.exe",
    "C:\ProgramData\Miniforge3\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python39\python.exe"
)

foreach ($c in $candidates) {
    if (Test-PythonExe $c) { $PythonExe = $c; break }
}

if (-not $PythonExe) {
    $pathCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pathCmd -and ($pathCmd.Source -notmatch "WindowsApps")) {
        if (Test-PythonExe $pathCmd.Source) { $PythonExe = $pathCmd.Source }
    }
}

if ($PythonExe) {
    $ver = & $PythonExe --version 2>&1
    Write-OK "Found Python: $ver"
    Write-Info "Path: $PythonExe"
} else {
    Write-Warn "Python not found. Downloading Miniforge3..."
    $MinForgeUrl  = "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe"
    $MinForgeFile = Join-Path $TempDir "Miniforge3-Windows-x86_64.exe"
    $MinForgeDir  = "C:\ProgramData\mambaforge"

    Write-Info "Downloading (this may take a minute)..."
    try {
        Invoke-WebRequest -Uri $MinForgeUrl -OutFile $MinForgeFile -UseBasicParsing
    } catch {
        Write-Fail "Download failed: $_"
        Write-Fail "Download manually from: $MinForgeUrl"
        pause; exit 1
    }

    Write-Info "Installing to $MinForgeDir ..."
    $proc = Start-Process $MinForgeFile -ArgumentList "/S /D=$MinForgeDir" -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Fail "Miniforge installer failed (exit code $($proc.ExitCode))."
        pause; exit 1
    }

    $PythonExe = "$MinForgeDir\python.exe"
    if (Test-PythonExe $PythonExe) {
        $ver = & $PythonExe --version 2>&1
        Write-OK "Python installed: $ver"
    } else {
        Write-Fail "Install finished but python.exe not found at expected path."
        pause; exit 1
    }
}


# ================================================================
# 2. ODBC DRIVER 17 FOR SQL SERVER
# ================================================================

Write-Step "2/4  Checking ODBC Driver 17 for SQL Server..."

$OdbcKey17 = "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server"
$OdbcKey18 = "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 18 for SQL Server"

if (Test-Path $OdbcKey17) {
    Write-OK "ODBC Driver 17 already installed."
} else {
    if (Test-Path $OdbcKey18) {
        Write-Warn "ODBC Driver 18 found but the app needs v17 - installing v17 alongside."
    }

    # Try winget first (built into Windows 11)
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    $installed = $false

    if ($wingetCmd) {
        Write-Info "Trying winget..."
        try {
            winget install --id "Microsoft.ODBCDriverForSQLServer" --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
            if (Test-Path $OdbcKey17) { $installed = $true; Write-OK "ODBC Driver 17 installed via winget." }
        } catch {}
    }

    if (-not $installed) {
        $OdbcUrl  = "https://go.microsoft.com/fwlink/?linkid=2249004"
        $OdbcFile = Join-Path $TempDir "msodbcsql17_x64.msi"

        Write-Info "Downloading ODBC Driver 17 from Microsoft..."
        try {
            Invoke-WebRequest -Uri $OdbcUrl -OutFile $OdbcFile -UseBasicParsing
        } catch {
            Write-Fail "Download failed: $_"
            Write-Fail "Download manually from:"
            Write-Fail "  https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server"
            pause; exit 1
        }

        Write-Info "Installing ODBC Driver 17..."
        $msiArgs = "/i `"$OdbcFile`" /quiet /norestart IACCEPTMSODBCSQLLICENSETERMS=YES"
        $proc = Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru

        if ($proc.ExitCode -eq 0) {
            Write-OK "ODBC Driver 17 installed."
        } elseif ($proc.ExitCode -eq 3010) {
            Write-Warn "ODBC Driver 17 installed - a reboot is required to finish."
        } else {
            Write-Fail "ODBC installer failed (exit code $($proc.ExitCode))."
            pause; exit 1
        }
    }
}


# ================================================================
# 3. PYTHON PACKAGES
# ================================================================

Write-Step "3/4  Installing Python packages..."

$ReqFile = Join-Path $ScriptDir "backend\requirements.txt"
if (-not (Test-Path $ReqFile)) {
    Write-Fail "requirements.txt not found at: $ReqFile"
    Write-Fail "Run this script from inside the project folder."
    pause; exit 1
}

Write-Info "Upgrading pip..."
& $PythonExe -m pip install --upgrade pip --quiet

Write-Info "Installing from backend\requirements.txt..."
& $PythonExe -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install failed - see errors above."
    pause; exit 1
}
Write-OK "All Python packages installed."


# ================================================================
# 4. .env FILE
# ================================================================

Write-Step "4/4  Checking .env configuration..."

$EnvFile    = Join-Path $ScriptDir ".env"
$EnvExample = Join-Path $ScriptDir ".env.example"

if (Test-Path $EnvFile) {
    Write-OK ".env already exists - skipping."
} elseif (Test-Path $EnvExample) {
    Copy-Item $EnvExample $EnvFile
    Write-Warn ".env created from .env.example."
    Write-Warn "IMPORTANT: Edit .env and fill in credentials before starting:"
    Write-Info "  MAST_SQL_USER       - your Windows domain\username"
    Write-Info "  MAST_SQL_PASSWORD   - your Windows password"
    Write-Info "  GILSON_SQL_PASSWORD - dashboard_reader password"
    Write-Info "  File: $EnvFile"
} else {
    Write-Warn ".env.example not found - create .env manually. See README.md."
}


# --- Patch start.bat to use the Python we found/installed ---

$StartBat = Join-Path $ScriptDir "start.bat"
if (Test-Path $StartBat) {
    $batContent = Get-Content $StartBat -Raw
    $oldPath    = "C:\ProgramData\mambaforge\python.exe"
    if ($batContent.Contains($oldPath) -and ($PythonExe -ne $oldPath)) {
        $batContent = $batContent.Replace($oldPath, $PythonExe)
        Set-Content $StartBat $batContent -NoNewline
        Write-Info "start.bat updated: PYTHON=$PythonExe"
    }
}


# ================================================================
# DONE
# ================================================================

Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Setup complete!"                                                 -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""

$needsEdit = (-not (Test-Path $EnvFile)) -or ((Get-Content $EnvFile -Raw) -match "YOUR_")
if ($needsEdit) {
    Write-Host "  ACTION REQUIRED: edit .env and set your SQL passwords." -ForegroundColor Yellow
    Write-Host "  File: $EnvFile"                                          -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "  When ready: double-click start.bat to launch the dashboard." -ForegroundColor Cyan
Write-Host ""
pause
