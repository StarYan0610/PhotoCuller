param(
    [string]$Python = (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found. Create .venv first, or pass -Python with an interpreter path."
}
$Python = (Resolve-Path -LiteralPath $Python).Path
Push-Location $PSScriptRoot
try {
    & $Python -m PyInstaller --noconfirm --clean PhotoCuller.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    Write-Host "Build output: $(Join-Path $PSScriptRoot 'dist\PhotoCuller')"
}
finally { Pop-Location }
