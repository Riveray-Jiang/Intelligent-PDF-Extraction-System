[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",

    [int]$Port = 8892
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }

Push-Location $repoRoot
try {
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    & $pythonExe -m backend.product_server --host $BindHost --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Product server failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
