@echo off
rem ================================================================
rem  WingMan - everyday launch script (Windows)
rem
rem  This is a thin alias for the single user entry point:
rem      wingman.cmd
rem  It exists for people who are used to scripts\run_dev.bat.
rem
rem  Safe defaults for end users:
rem    * no --reload (a reloading server restarts randomly and shows
rem      scary tracebacks; developers can opt in manually, see below)
rem    * the launcher creates backend\.venv and installs
rem      backend\requirements.lock.txt on first run
rem    * the browser opens automatically (use --no-browser to skip)
rem
rem  Developers who want auto-reload:
rem      backend\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8787
rem
rem  Any argument is forwarded to wingman.cmd, e.g.
rem      scripts\run_dev.bat --port 8788
rem      scripts\run_dev.bat --setup-only
rem      scripts\run_dev.bat --doctor
rem
rem  Exit codes: 0 ok | 2 preflight refused | 3 dependency setup failed | 1 other
rem ================================================================

call "%~dp0..\wingman.cmd" %*
exit /b %ERRORLEVEL%
