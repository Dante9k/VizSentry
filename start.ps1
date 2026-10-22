param([string]$DataDir, [int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\runtime.ps1')
Set-Location -LiteralPath $PSScriptRoot
Set-VizRuntime -ProjectRoot $PSScriptRoot -DataDir $DataDir
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
    throw 'Application dependencies or web build are missing. Run .\setup.ps1 first.'
}
$env:VIZSENTRY_PORT = $Port.ToString()
Write-Host "VizSentry: http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "Local data: $env:VIZSENTRY_DATA_DIR"
Write-Host 'Keep this terminal open. Ctrl+C stops the service; saved jobs can be resumed next time.'
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
Invoke-VizCommand -Executable $venvPython -Arguments @('-m', 'vizsentry')
