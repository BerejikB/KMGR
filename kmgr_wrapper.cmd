@echo off
setlocal
set "ROOT=K:\GOOSE\KMGR"
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "SERVER=%ROOT%\server.py"
set "FS_PS=%ROOT%\filestream\start_fs.ps1"

if not exist "%PY%" (
  py -3.13 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || exit /b 1
  "%PY%" -m pip -q install -U pip
  "%PY%" -m pip -q install "mcp[cli]>=1.2.0"
)

REM --- Start file-stream server (non-blocking); logs in KMGR\scratch ---
if exist "%FS_PS%" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%FS_PS%" -Port 17777 -Roots "K:\GOOSE" -MaxChunk 65536 -MaxLines 400 1>nul 2>>"%ROOT%\scratch\filestream_boot.err"
)

REM --- diagnostics (write once at boot; capture python tracebacks) ---
set "KMGR_ROOT=%ROOT%"
set "KMGR_BOOTLOG=1"
set "PYTHON_EXE=%PY%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM Ensure runtime deps compatible with server (Pydantic v1)
"%PY%" -m pip -q install "pydantic<2" >nul 2>&1

REM Keep STDOUT for MCP; capture STDERR to a file
"%PY%" "%SERVER%" 2> "%ROOT%\scratch\kmgr_err.log"
