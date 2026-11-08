@echo off
rem One-click start on Windows: calls scripts/demo_up.sh
rem Requires Git for Windows (it ships bash.exe).
setlocal
cd /d "%~dp0.."

set "BASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined BASH if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not defined BASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "BASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not defined BASH for %%i in (bash.exe) do if not "%%~$PATH:i"=="" set "BASH=%%~$PATH:i"

if not defined BASH (
  echo [ERROR] bash.exe not found. Please install Git for Windows first:
  echo         https://git-scm.com/download/win
  pause
  exit /b 1
)

echo [START] CulturalWalk demo stack ^(first run installs deps, may take minutes^)
"%BASH%" scripts/demo_up.sh
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [EXIT %RC%] Startup failed. Send the output above to the maintainer.
  pause
)
exit /b %RC%
