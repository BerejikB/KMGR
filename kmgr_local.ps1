# Runs the MCP server in the foreground (for local sanity checks).
$ErrorActionPreference = 'Stop'
$ROOT   = 'K:\GOOSE\KMGR'
$VENV   = Join-Path $ROOT '.venv'
$PY     = Join-Path $VENV 'Scripts\python.exe'
$SERVER = Join-Path $ROOT 'server.py'

if (-not (Test-Path $SERVER)) { throw "server.py not found at $SERVER" }

if (-not (Test-Path $PY)) {
  Write-Host "[KMGR] Creating venv at $VENV"
  try {
    py -3.13 -m venv $VENV
  } catch {
    python -m venv $VENV
  }
  & $PY -m pip install -U pip
  & $PY -m pip install "mcp[cli]>=1.2.0"
}

$env:KMGR_ROOT = $ROOT
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
& $PY $SERVER
