$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:UV_SYSTEM_CERTS = 'true'
$env:PYTHONUTF8 = '1'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvExe = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvExe)) { throw 'Install uv from https://docs.astral.sh/uv/getting-started/installation/ then run this installer again.' }
if (-not (Test-Path -LiteralPath '.venv-voice/Scripts/python.exe')) {
    & $uvExe venv --python 3.12 .venv-voice
    if ($LASTEXITCODE -ne 0) { throw 'Voice environment creation failed.' }
}
& $uvExe pip install --python .venv-voice/Scripts/python.exe torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed.' }
& $uvExe pip install --python .venv-voice/Scripts/python.exe -r requirements-voice.txt --override scripts/voice-constraints.txt
if ($LASTEXITCODE -ne 0) { throw 'Speech dependencies installation failed.' }
& .venv-voice/Scripts/python.exe -u scripts/download_voice_models.py
if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Run again to resume.' }
& .venv-voice/Scripts/python.exe -c "import torch; assert torch.cuda.is_available(), 'CUDA GPU unavailable'; print(torch.cuda.get_device_name(0))"
if ($LASTEXITCODE -ne 0) { throw 'CUDA GPU check failed.' }
Write-Host 'Local voice environment is ready. Open the video workshop and choose a local voice.'
