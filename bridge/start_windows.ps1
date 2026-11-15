# Запуск USB-моста на Windows (двойной клик по start_windows.bat).
# Arduino/ESP32 подключается кабелем Type-C -> в системе появляется COM-порт.
# Если есть Python — используется usb_bridge.py (pyserial ставится в bridge\venv один раз).
# Если Python нет — автоматически запускается bridge_windows.ps1, ему ничего устанавливать не нужно.

$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Find-Python {
    $candidates = @(
        @{ Exe = "python"; Prefix = @() },
        @{ Exe = "py";     Prefix = @("-3") }
    )
    foreach ($cand in $candidates) {
        $cmd = Get-Command $cand.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $testArgs = @($cand.Prefix) + @("--version")
        try {
            & $cmd.Source @testArgs *> $null
            if ($LASTEXITCODE -eq 0) { return @{ Exe = $cmd.Source; Prefix = $cand.Prefix } }
        } catch { }
    }
    return $null
}

$py = Find-Python

if ($py) {
    $venvDir = Join-Path $PSScriptRoot "venv"
    $venvPy = Join-Path $venvDir "Scripts\python.exe"

    if (-not (Test-Path -LiteralPath $venvPy)) {
        Write-Host "Первый запуск: создаю окружение и устанавливаю pyserial..."
        $venvArgs = @($py.Prefix) + @("-m", "venv", $venvDir)
        & $py.Exe @venvArgs
    }

    if (Test-Path -LiteralPath $venvPy) {
        & $venvPy -m pip install --quiet --disable-pip-version-check pyserial
        & $venvPy (Join-Path $PSScriptRoot "usb_bridge.py") @args
        exit $LASTEXITCODE
    }
    Write-Host "Не удалось создать venv — запускаю версию на PowerShell."
} else {
    Write-Host "Python не найден — запускаю версию на PowerShell (ничего устанавливать не нужно)."
}

& (Join-Path $PSScriptRoot "bridge_windows.ps1") @args
exit $LASTEXITCODE
