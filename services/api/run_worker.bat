@echo off
cd /d "%~dp0"
set DATABASE_URL=sqlite:///./demo.db
".venv\Scripts\python.exe" -m app.modules.planner.worker
