# Rebuild helper for PowerShell: activates venv (if present) and runs the faster spec-based PyInstaller build
$venvActivate = Join-Path -Path (Join-Path -Path $PSScriptRoot -ChildPath '.venv') -ChildPath 'Scripts\Activate.ps1'
if (Test-Path $venvActivate) {
    Write-Host "Activating venv..."
    & $venvActivate
} else {
    Write-Host "No venv found at .venv — proceeding without activation."
}

Write-Host "Running PyInstaller (spec)..."
pyinstaller --noconfirm --onedir SaveFinder.spec
Write-Host "Rebuild finished. See dist\SaveFinder\"
