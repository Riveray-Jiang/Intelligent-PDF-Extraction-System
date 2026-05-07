param(
  [string]$Manifest = "benchmarks/mineru_fast_upgrade_v2.yaml",
  [string]$OutputDir = "reports/mineru_fast_upgrade_v2",
  [switch]$BuildMinerU31,
  [switch]$KeepServices,
  [int]$ColdRuns = 0,
  [int]$WarmRuns = 5
)

$ErrorActionPreference = "Stop"

if ($BuildMinerU31) {
  docker build `
    -f docker/Dockerfile.mineru31 `
    -t pdf-agent/mineru-runner:3.1.3 `
    --build-arg MINERU_VERSION=3.1.3 `
    .
}

$PythonExe = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
  $PythonExe = "python"
}

$argsList = @(
  "-m", "backend.mineru_fast_upgrade_benchmark",
  "--manifest", $Manifest,
  "--output-dir", $OutputDir,
  "--cold-runs", "$ColdRuns",
  "--warm-runs", "$WarmRuns"
)

if ($KeepServices) {
  $argsList += "--keep-services"
}

& $PythonExe @argsList
