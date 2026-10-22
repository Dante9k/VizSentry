param([string]$DataDir, [switch]$SkipModels, [switch]$SkipBrowser)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\runtime.ps1')
Set-Location -LiteralPath $PSScriptRoot
Set-VizRuntime -ProjectRoot $PSScriptRoot -DataDir $DataDir
$pnpm = Find-VizRuntime 'pnpm' 'bin\fallback\pnpm.cmd'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonRuntime = Find-VizRuntime 'python' 'python\python.exe'
    Invoke-VizCommand -Executable $pythonRuntime -Arguments @('-m', 'venv', '.venv')
}
Invoke-VizCommand -Executable $venvPython -Arguments @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "Python 3.12+ is required by the locked NumPy dependency. Recreate .venv with Python 3.12+.")')
Invoke-VizCommand -Executable $venvPython -Arguments @('-m', 'pip', 'install', '-r', 'requirements.lock.txt')
Invoke-VizCommand -Executable $venvPython -Arguments @('-m', 'pip', 'install', '--no-deps', '-e', '.')
foreach ($package in @('frontend', 'connector', 'browser')) {
    Push-Location -LiteralPath (Join-Path $PSScriptRoot $package)
    try { Invoke-VizCommand -Executable $pnpm -Arguments @('install', '--frozen-lockfile') }
    finally { Pop-Location }
}
foreach ($package in @('frontend', 'connector')) {
    Push-Location -LiteralPath (Join-Path $PSScriptRoot $package)
    try { Invoke-VizCommand -Executable $pnpm -Arguments @('run', 'build') }
    finally { Pop-Location }
}
if (-not $SkipBrowser) {
    Push-Location -LiteralPath (Join-Path $PSScriptRoot 'browser')
    try { Invoke-VizCommand -Executable $pnpm -Arguments @('run', 'install-browser') }
    finally { Pop-Location }
}
if (-not $SkipModels) {
    Invoke-VizCommand -Executable $venvPython -Arguments @('-m', 'pip', 'install', '-r', 'requirements-models.txt')
    Invoke-VizCommand -Executable $venvPython -Arguments @('scripts\prepare_models.py', '--data-dir', $env:VIZSENTRY_DATA_DIR)
}
Write-Host 'Setup complete. Run start.cmd or .\start.ps1' -ForegroundColor Green
Write-Host "Local data: $env:VIZSENTRY_DATA_DIR"
