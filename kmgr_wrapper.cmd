@echo off
setlocal
set "ROOT=K:\GOOSE\KMGR"
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "SERVER=%ROOT%\server.py"
set "FS_PS=%ROOT%\filestream\server\file-stream-server.ps1"

REM --- Choose a free file-stream port (prefer 17777; scan +9; else random 20000-65000) ---
set "FS_BASE_PORT=17777"
set "FS_SCAN=10"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$base=%FS_BASE_PORT%;$n=%FS_SCAN%;$free=$null; for($i=0;$i -lt $n;$i++){ $p=$base+$i; try { $l=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$p); $l.Start(); $l.Stop(); $free=$p; break } catch {} }; if(-not $free){ $free=Get-Random -Minimum 20000 -Maximum 65000 }; Write-Output $free"`) do set "FS_PORT=%%P"
if not defined FS_PORT set "FS_PORT=%FS_BASE_PORT%"
set "FILE_STREAM_PORT=%FS_PORT%"

if not exist "%ROOT%\scratch" mkdir "%ROOT%\scratch" 1>nul 2>nul
echo %FS_PORT%>"%ROOT%\scratch\filestream.port"

if not exist "%PY%" (
  py -3.13 -m venv "%VENV%" 1>nul 2>nul || python -m venv "%VENV%" 1>nul 2>nul || exit /b 1
  "%PY%" -m pip -q install -U pip >nul 2>&1
  "%PY%" -m pip -q install "mcp[cli]>=1.2.0" >nul 2>&1
)

REM --- Start file-stream server (non-blocking); logs in KMGR\scratch ---
if exist "%FS_PS%" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%FS_PS%" -Port %FS_PORT% -Roots "K:\GOOSE" -MaxChunk 65536 -MaxLines 400 1>nul 2>>"%ROOT%\scratch\filestream_boot.err"
)

REM --- diagnostics (write once at boot; capture python tracebacks) ---
set "KMGR_ROOT=%ROOT%"
set "KMGR_BOOTLOG=1"
set "PYTHON_EXE=%PY%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM Ensure MCP CLI present (avoid slow installs on every launch)
"%PY%" -c "import importlib, sys; sys.exit(0 if importlib.util.find_spec('mcp') else 1)" 2>nul || "%PY%" -m pip -q install "mcp[cli]>=1.2.0" >nul 2>&1

REM Ensure runtime deps compatible with MCP (Pydantic v2)
"%PY%" -m pip -q install "pydantic>=2,<3" >nul 2>&1

REM Keep STDOUT for MCP; capture STDERR to a file
"%PY%" "%SERVER%" 2> "%ROOT%\scratch\kmgr_err.log"
