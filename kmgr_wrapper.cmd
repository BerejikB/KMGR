@echo off
setlocal

REM ---- configurable root (all data stays on K:) ----
set "ROOT=K:\GOOSE\KMGR"
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "SERVER=%ROOT%\server.py"

REM ---- sanity checks ----
if not exist "%SERVER%" (
  echo [KMGR] server.py not found at "%SERVER%"
  exit /b 1
)

REM ---- bootstrap venv on K:\ if missing ----
if not exist "%PY%" (
  echo [KMGR] Creating venv at "%VENV%"
  rem Prefer py launcher; fallback to python on PATH
  py -3.13 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || (
    echo [KMGR] Failed to create venv. Ensure Python 3.13+ is installed.
    exit /b 1
  )
  "%PY%" -m pip install -U pip || (
    echo [KMGR] pip upgrade failed
    exit /b 1
  )
  "%PY%" -m pip install "mcp[cli]>=1.2.0" || (
    echo [KMGR] failed to install mcp[cli]
    exit /b 1
  )
)

REM ---- runtime env (stdio-safe) ----
set "KMGR_ROOT=%ROOT%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM ---- hand control to the MCP server (foreground for STDIO) ----
"%PY%" "%SERVER%"
