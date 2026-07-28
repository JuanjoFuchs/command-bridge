@echo off
REM PATH shim — resolves the repo venv so `vm` works from any directory with no cd.
setlocal
set "VM_ROOT=%~dp0.."
if exist "%VM_ROOT%\venv\Scripts\python.exe" (
  set "VM_PY=%VM_ROOT%\venv\Scripts\python.exe"
) else (
  set "VM_PY=python"
)
"%VM_PY%" -c "import sys; sys.path.insert(0, r'%VM_ROOT%'); from vm.cli import main; raise SystemExit(main())" %*
