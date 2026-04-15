$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot "..\\.venv310\\Scripts\\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python 3.10 virtual environment not found at $venvPython"
}

& $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt") pyinstaller

Push-Location $projectRoot
try {
    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "MadrixHueBridgeUI" `
        --collect-all hue_entertainment_pykit `
        --collect-all mbedtls `
        "madrix_hue_bridge_gui.py"
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Build complete:"
Write-Host (Join-Path $projectRoot "dist\\MadrixHueBridgeUI.exe")
