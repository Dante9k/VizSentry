$ErrorActionPreference = 'Stop'

function Find-VizRuntime {
    param([string]$Name, [string]$BundledRelative)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*WindowsApps*') { return $command.Source }
    $bundled = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\$BundledRelative"
    if (Test-Path -LiteralPath $bundled) { return $bundled }
    throw "Missing $Name. Install Python 3.12+, Node.js 20.19+ or 22.12+, and pnpm, then retry."
}

function Set-VizRuntime {
    param([string]$ProjectRoot, [string]$DataDir)
    if (-not $DataDir) {
        if ($env:VIZSENTRY_DATA_DIR) { $DataDir = $env:VIZSENTRY_DATA_DIR }
        else { $DataDir = Join-Path $ProjectRoot '.data' }
    }
    $env:VIZSENTRY_DATA_DIR = [System.IO.Path]::GetFullPath($DataDir)
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:VIZSENTRY_DATA_DIR 'browsers'
    $env:VIZSENTRY_NODE = Find-VizRuntime 'node' 'node\bin\node.exe'
    $env:PATH = (Split-Path $env:VIZSENTRY_NODE) + [IO.Path]::PathSeparator + $env:PATH
    $env:PYTHONUTF8 = '1'
    $env:PYTHONPATH = Join-Path $ProjectRoot 'backend'
}

function Invoke-VizCommand {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable" }
}
