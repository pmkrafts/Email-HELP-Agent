@echo off
rem One-command runner (Windows cmd):  run.bat [run.py options]
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run.py %*
) else (
  python run.py %*
)
endlocal
