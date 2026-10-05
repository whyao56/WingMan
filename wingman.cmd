@echo off
rem ================================================================
rem  WingMan launcher - the single user entry point (Windows)
rem
rem  Usage:  wingman.cmd [--port N] [--setup-only]
rem                      [--doctor] [--no-browser] [--help]
rem
rem  This file is intentionally ASCII-only: cmd.exe echoes raw bytes
rem  using the console's active code page, so Chinese text placed here
rem  would be mojibake on either a cp936 or a cp65001 console.
rem  All user-facing text is printed by scripts\bootstrap.ps1, which
rem  switches the console to UTF-8 (chcp 65001) and restores the
rem  original code page on every exit path (including Ctrl+C).
rem
rem  Exit codes: 0 ok | 2 preflight refused | 3 dependency setup failed
rem              1 other error
rem ================================================================

setlocal EnableExtensions
set "WINGMAN_ROOT=%~dp0"
set "WINGMAN_PS1=%WINGMAN_ROOT%scripts\bootstrap.ps1"
set "WINGMAN_ARGS=%*"

if not exist "%WINGMAN_PS1%" (
  echo.
  echo [FATAL] WingMan is incomplete: scripts\bootstrap.ps1 is missing.
  echo         Expected at: %WINGMAN_PS1%
  echo         Re-clone or re-extract the whole repository, then run wingman.cmd again.
  echo.
  exit /b 1
)

rem Windows PowerShell 5.1 ships with every Windows 10/11, so it is the
rem default. WINGMAN_POWERSHELL lets power users force PowerShell 7 (pwsh).
set "WINGMAN_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not "%WINGMAN_POWERSHELL%"=="" set "WINGMAN_PS=%WINGMAN_POWERSHELL%"
if not exist "%WINGMAN_PS%" (
  echo.
  if not "%WINGMAN_POWERSHELL%"=="" (
    echo [FATAL] WINGMAN_POWERSHELL points to a file that does not exist:
    echo         %WINGMAN_PS%
    echo         Unset WINGMAN_POWERSHELL, or point it at powershell.exe / pwsh.exe.
  ) else (
    echo [FATAL] Windows PowerShell was not found at:
    echo         %WINGMAN_PS%
    echo         WingMan needs Windows PowerShell 5.1 ^(preinstalled on Windows 10/11^).
  )
  echo.
  exit /b 1
)

rem Remember the console code page as a safety net: even if PowerShell is
rem killed hard, this batch file restores the user's original code page.
set "WINGMAN_OLDCP="
for /f "usebackq tokens=2 delims=:" %%c in (`chcp`) do set "WINGMAN_OLDCP=%%c"
if defined WINGMAN_OLDCP set "WINGMAN_OLDCP=%WINGMAN_OLDCP: =%"

rem -NonInteractive: ?????????????????? Windows PowerShell 5.1
rem ? Ctrl+Break ???? "[DBG]: PS ..." ??????????????
"%WINGMAN_PS%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%WINGMAN_PS1%"
set "WINGMAN_RC=%ERRORLEVEL%"

if defined WINGMAN_OLDCP (
  if not "%WINGMAN_OLDCP%"=="65001" chcp %WINGMAN_OLDCP% >nul 2>nul
)

exit /b %WINGMAN_RC%
