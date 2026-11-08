@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

rem Pick a Python: explicit override > bundled runtime > installed launcher.
set "PY_CMD="
if defined CULTURALWALK_PYTHON set PY_CMD="%CULTURALWALK_PYTHON%"
if not defined PY_CMD if exist "%ROOT%runtime\python.exe" set PY_CMD="%ROOT%runtime\python.exe"
if not defined PY_CMD set PY_CMD=py -3.12
if not defined PY_CMD set PY_CMD=python

set "PYTHONPATH=%ROOT%site-packages;%ROOT%"
set "DATABASE_URL=sqlite:///./data/demo.db"
set "WEB_DIST=%ROOT%web"
set "MEDIA_STORAGE_ROOT=%ROOT%data/media"
set "ENV=local"

if not exist "%ROOT%data" mkdir "%ROOT%data"

%PY_CMD% -c "import fastapi, uvicorn, sqlalchemy, alembic" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python dependencies not found.
  echo         Install Python 3.12 or set CULTURALWALK_PYTHON to a python.exe,
  echo         then run: %PY_CMD% -m pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist "%ROOT%data\demo.db" (
  echo [1/3] Creating database...
  %PY_CMD% -m alembic -c alembic.ini upgrade head || goto :fail
  echo [2/3] Loading demo data...
  %PY_CMD% scripts\seed_demo.py --database-url "sqlite:///./data/demo.db" || goto :fail
) else (
  echo [1/3] Reusing existing database: data\demo.db
)

echo [3/3] Serving http://127.0.0.1:8000  ^(close this window to stop^)
start "" /b %PY_CMD% -m app.modules.planner.worker
start "" /b %PY_CMD% -c "import time, webbrowser; time.sleep(3); webbrowser.open('http://127.0.0.1:8000')"
%PY_CMD% -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit /b 0

:fail
echo.
echo [ERROR] Startup failed, see messages above.
pause
exit /b 1
