@echo off
REM PATH shim for cmd.exe and PowerShell. Same contract as bin/vm: identical behaviour from
REM any cwd, with no env vars set and no venv activated.
REM
REM ASCII ONLY, CRLF ONLY. cmd.exe parses a batch file by BYTE OFFSET, so a single multi-byte
REM character anywhere -- even inside a comment -- shifts every line after it and the shim
REM starts executing fragments of its own source ("'tlocal' is not recognized"). An em-dash in
REM one comment broke this file; that is why the prose here is plain.
setlocal

REM %~dp0 ends with a backslash, so "%~dp0.." is the bin directory's parent. Canonicalise it
REM through %%~fI so the path in any error message is the one a person would recognise.
for %%I in ("%~dp0..") do set "VM_ROOT=%%~fI"

REM A shim copied out of the checkout cannot find its repo. Say so, rather than letting the
REM interpreter emit a "can't open file" that reads like a broken install.
if not exist "%VM_ROOT%\bin\vm-run.py" (
  echo {"error": "vm shim cannot find its repo", "code": "detached_shim", "remedy": "keep this file inside the checkout's bin directory and put that directory on PATH; do not copy it elsewhere"} 1>&2
  exit /b 2
)

REM Prefer the repo venv: the dependencies live there and nowhere else, so a bare `python` gets
REM an ImportError three frames deep instead of a CLI.
if exist "%VM_ROOT%\venv\Scripts\python.exe" (
  set "VM_PY=%VM_ROOT%\venv\Scripts\python.exe"
) else (
  where python >nul 2>&1 || (
    echo {"error": "no python interpreter found", "code": "no_python", "remedy": "install Python 3.11+, then from the repo root run: python -m venv venv  and  venv\Scripts\python -m pip install -r requirements.txt"} 1>&2
    exit /b 2
  )
  set "VM_PY=python"
)

REM No `python -c` and no path splicing: the launcher resolves the repo root from its own
REM __file__, so the only thing crossing the shell boundary is one file path.
"%VM_PY%" "%VM_ROOT%\bin\vm-run.py" %*
exit /b %ERRORLEVEL%
