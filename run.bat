@echo off
rem DSH-Bench entry for Windows. Run inside Command Prompt, e.g.  run.bat doctor
chcp 65001 >nul
set PYTHONUTF8=1
python "%~dp0bench\bench.py" %*
