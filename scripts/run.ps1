<#
.SYNOPSIS
  Starts everything needed for the race replay UI and opens it in a browser.

.DESCRIPTION
  Starts Docker Desktop and Postgres if they are not already up, applies any pending
  migrations, launches the API, then opens http://localhost:8000/replay.
  Ctrl+C stops the server. Postgres keeps running unless -StopDb is passed.

.EXAMPLE
  .\scripts\run.ps1            # start and open the UI
  .\scripts\run.ps1 -Sync      # pull new workouts from Concept2 first
  .\scripts\run.ps1 -NoBrowser # start the server without opening a browser
#>
[CmdletBinding()]
param(
  [switch]$Sync,       # pull new/edited workouts and their strokes before starting
  [switch]$Recompute,  # re-run classification and metrics before starting
  [switch]$NoBrowser,
  [switch]$StopDb,     # stop the Postgres container when the server exits
  [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }

# Native tools (uv, alembic, docker) log progress to stderr. Piping that while
# ErrorActionPreference is Stop turns ordinary output into a terminating error, so
# run them plainly and judge success by the exit code.
function Invoke-Native([string]$what, [scriptblock]$command) {
  & $command
  if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

# A plain socket check: HTTP probes against "localhost" can stall on proxy lookups or
# resolve to ::1 while uvicorn listens on 127.0.0.1.
function Wait-ForPort([int]$port, [int]$timeoutSeconds, $process) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if ($process -and $process.HasExited) { return $false }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
      $client.Connect("127.0.0.1", $port)
      if ($client.Connected) { return $true }
    } catch {
    } finally { $client.Dispose() }
    Start-Sleep -Milliseconds 300
  }
  return $false
}

# --- prerequisites -----------------------------------------------------------

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/"
}

if (-not (Test-Path .env)) {
  Warn ".env is missing. Copy .env.example to .env and fill in your Concept2 client id/secret."
  throw "missing .env"
}

# --- docker ------------------------------------------------------------------

Step "Checking Docker"
docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) {
  $desktop = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
  if (-not (Test-Path $desktop)) { throw "Docker is not running and Docker Desktop was not found at $desktop" }
  Warn "Docker is not running. Starting Docker Desktop (this can take a minute)..."
  Start-Process $desktop | Out-Null
  $deadline = (Get-Date).AddMinutes(3)
  do {
    Start-Sleep -Seconds 3
    docker info --format '{{.ServerVersion}}' *> $null
    $ready = $LASTEXITCODE -eq 0
  } while (-not $ready -and (Get-Date) -lt $deadline)
  if (-not $ready) { throw "Docker did not start in time. Start Docker Desktop, then run this again." }
}

Step "Starting Postgres"
Invoke-Native "docker compose up" { docker compose up -d }
$deadline = (Get-Date).AddMinutes(2)
do {
  docker compose exec -T db pg_isready -U erg *> $null
  $ready = $LASTEXITCODE -eq 0
  if (-not $ready) { Start-Sleep -Seconds 2 }
} while (-not $ready -and (Get-Date) -lt $deadline)
if (-not $ready) { throw "Postgres did not become ready. Try: docker compose logs db" }

# --- python + schema ---------------------------------------------------------

Step "Syncing dependencies"
Invoke-Native "uv sync" { uv sync --quiet }

Step "Applying database migrations"
Invoke-Native "alembic upgrade" { uv run alembic upgrade head }

if ($Sync) {
  Step "Pulling new workouts from Concept2"
  Invoke-Native "erg sync" { uv run erg sync }
  $Recompute = $true  # new workouts need classifying and measuring
}

if ($Recompute) {
  Step "Recomputing classification and metrics"
  Invoke-Native "erg classify" { uv run erg classify }
  Invoke-Native "erg metrics" { uv run erg metrics }
}

# --- server ------------------------------------------------------------------

$url = "http://localhost:$Port/replay"
Step "Starting the API on port $Port"

$server = Start-Process -FilePath "uv" `
  -ArgumentList @("run", "uvicorn", "erg.api:app", "--host", "127.0.0.1", "--port", "$Port") `
  -PassThru -NoNewWindow

try {
  $up = Wait-ForPort -port $Port -timeoutSeconds 60 -process $server

  if ($server.HasExited) { throw "The API exited on startup. Run 'uv run uvicorn erg.api:app' to see the error." }
  if (-not $up) { throw "The API did not respond on port $Port." }

  # A fresh database has no athlete yet: send them through the Concept2 login first.
  $authorized = $false
  try {
    $workouts = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/workouts?limit=1" -TimeoutSec 10
    $authorized = @($workouts).Count -gt 0
  } catch { $authorized = $false }

  if (-not $authorized) {
    $url = "http://localhost:$Port/auth/login"
    Warn "No workouts found yet. Opening the Concept2 login; after that, run this script with -Sync."
  }

  Write-Host ""
  Write-Host "    Race replay:  http://localhost:$Port/replay" -ForegroundColor Green
  Write-Host "    API docs:     http://localhost:$Port/docs"
  Write-Host "    Press Ctrl+C to stop." -ForegroundColor DarkGray
  Write-Host ""

  if (-not $NoBrowser) { Start-Process $url | Out-Null }

  $server.WaitForExit()
} finally {
  if (-not $server.HasExited) {
    Step "Stopping the API"
    $server.Kill()
  }
  if ($StopDb) {
    Step "Stopping Postgres"
    docker compose stop | Out-Null
  }
}
