[CmdletBinding()]
param(
    [switch]$Base,
    [switch]$Flash,
    [string]$PythonExe = "python",
    [switch]$SkipPipUpgrade,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$Command
    )
    Write-Host "==> $Description" -ForegroundColor Cyan
    Write-Host "    $Command"
    if (-not $DryRun) {
        Invoke-Expression $Command
    }
}

function Ensure-Venv {
    param(
        [Parameter(Mandatory = $true)][string]$Name
    )
    $pythonPath = Join-Path $Name "Scripts\python.exe"
    if (Test-Path $pythonPath) {
        Write-Host "==> Reusing existing venv: $Name" -ForegroundColor Yellow
        return $pythonPath
    }
    Invoke-Step -Description "Create venv $Name" -Command "$PythonExe -m venv $Name"
    return $pythonPath
}

function Install-BaseEnv {
    $venvName = ".venv"
    $pythonPath = Ensure-Venv -Name $venvName
    if (-not $SkipPipUpgrade) {
        Invoke-Step -Description "Upgrade pip in $venvName" -Command "& `"$pythonPath`" -m pip install --upgrade pip"
    }
    Invoke-Step -Description "Install project requirements in $venvName" -Command "& `"$pythonPath`" -m pip install -r requirements.txt"
    Write-Host "==> Base env ready: $venvName" -ForegroundColor Green
}

function Install-FlashEnv {
    $venvName = ".venv-fa2-win283"
    $pythonPath = Ensure-Venv -Name $venvName
    if (-not $SkipPipUpgrade) {
        Invoke-Step -Description "Upgrade pip/setuptools/wheel in $venvName" -Command "& `"$pythonPath`" -m pip install --upgrade pip setuptools wheel"
    }
    Invoke-Step -Description "Install torch 2.8.0+cu129 in $venvName" -Command "& `"$pythonPath`" -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu129"
    Invoke-Step -Description "Install prebuilt flash-attn wheel in $venvName" -Command "& `"$pythonPath`" -m pip install `"https://github.com/LDNKS094/flash_attn_windows_2.8.3/releases/download/v2.8.3/flash_attn-2.8.3%2Btorch2.8.0cu129-cp312-cp312-win_amd64.whl`""
    Invoke-Step -Description "Install Qwen transcription deps in $venvName" -Command "& `"$pythonPath`" -m pip install qwen-asr transformers numpy soundfile librosa python-dotenv aiohttp==3.13.2"
    Write-Host "==> Flash env ready: $venvName" -ForegroundColor Green
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

if (-not $Base -and -not $Flash) {
    $Base = $true
    $Flash = $true
}

if ($Base) {
    Install-BaseEnv
}
if ($Flash) {
    Install-FlashEnv
}

Write-Host "Done." -ForegroundColor Green
