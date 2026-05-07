[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputPdf,

    [Parameter(Mandatory = $true)]
    [string]$OutputDir,

    [ValidateSet("all", "outline", "pagerange")]
    [string]$SelectionMode = "all",

    [string]$Selection,

    [switch]$RenderThumbnails,

    [int]$MaxParseAttempts = 1,

    [int]$MaxRerunAttempts = 0,

    [int]$MaxCascadeAttempts = 1
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    throw "docker.exe was not found on PATH"
}

try {
    & $dockerCmd.Source version 1>$null 2>$null
}
catch {
}
if ($LASTEXITCODE -ne 0) {
    throw "docker daemon is not reachable; start Docker Desktop or the Docker service first"
}

$args = @(
    "-m", "backend.pipeline_graph",
    "--input", $InputPdf,
    "--engine", "mineru",
    "--selection-mode", $SelectionMode,
    "--output-dir", $OutputDir,
    "--engine-config", "configs/engines_prod.yaml",
    "--max-parse-attempts", $MaxParseAttempts,
    "--max-rerun-attempts", $MaxRerunAttempts,
    "--cascade-enabled",
    "--cascade-engine", "paddle",
    "--cascade-engine-config", "configs/engines_prod.yaml",
    "--max-cascade-attempts", $MaxCascadeAttempts
)

if ($Selection) {
    $args += @("--selection", $Selection)
}

if ($RenderThumbnails) {
    $args += "--render-thumbnails"
}

Push-Location $repoRoot
try {
    & $pythonExe @args
    if ($LASTEXITCODE -ne 0) {
        throw "Pipeline failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}