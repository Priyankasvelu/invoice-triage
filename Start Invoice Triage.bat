@echo off
rem Double-click to open Invoice Triage in your browser. Keep the window open while you use it.
setlocal
cd /d "%~dp0"
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Invoice Triage needs Python 3.10 or newer.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
  echo then double-click this file again.
  pause
  exit /b 1
)
title Invoice Triage
%PY% -m invoice_triage
if errorlevel 1 pause
