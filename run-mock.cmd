@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 (
    echo run-mock.cmd: cannot enter "%~dp0".
    exit /b 1
)

where python.exe >nul 2>&1
if errorlevel 1 (
    echo run-mock.cmd: Python was not found on PATH.
    exit /b 1
)

if not exist "%~dp0tools\make_test_profile.py" (
    echo run-mock.cmd: tools\make_test_profile.py is missing.
    echo Restore the complete handoff archive before running the mock.
    exit /b 1
)

echo.
echo Refreshing the mock Freebuff profile for this folder...
python.exe "%~dp0tools\make_test_profile.py" --root "%~dp0testprofile" --port 8771
if errorlevel 1 (
    echo.
    echo run-mock.cmd: could not create the mock profile.
    exit /b 1
)

echo.
echo Mock data : %~dp0testprofile\.config\freebuff-desktop
echo Mock URL  : http://127.0.0.1:8771
echo Real Freebuff profiles are not used.
echo.
python.exe -u "%~dp0fb-dashboard.py" --config "%~dp0testprofile\config.json" %*
set "CODE=%ERRORLEVEL%"
echo.
echo run-mock.cmd: fb-dashboard.py exited with %CODE%.
exit /b %CODE%
