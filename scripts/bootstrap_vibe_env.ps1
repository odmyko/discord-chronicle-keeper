param(
    [string]$VenvPath = ".venv-vibe",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

Write-Host "[bootstrap-vibe] creating venv: $VenvPath"
& $PythonExe -m venv $VenvPath

$venvPython = Join-Path $VenvPath "Scripts\python.exe"

Write-Host "[bootstrap-vibe] upgrading pip"
& $venvPython -m pip install --upgrade pip

Write-Host "[bootstrap-vibe] installing torch (CUDA 12.9)"
& $venvPython -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu129

Write-Host "[bootstrap-vibe] installing transformers>=5 for VibeVoice"
& $venvPython -m pip install "transformers>=5.0.0" "accelerate" "soundfile" "librosa"

Write-Host "[bootstrap-vibe] done"
Write-Host "Run test:"
Write-Host "  $venvPython scripts/test_vibevoice_asr.py --audio <path-to-audio.mp3>"
