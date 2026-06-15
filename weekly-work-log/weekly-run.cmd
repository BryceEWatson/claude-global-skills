@echo off
REM weekly-work-log: the deterministic Sunday-night run.
REM Discovers last week's handoffs, refreshes + verifies the data, and opens a
REM review PR if the committed data changed. NEVER pushes main, never deploys.
REM Assumes the repo is on a clean main. Logs to .local-state\weekly.log.
setlocal
set "REPO=C:\Users\Bryce\Projects\brycewatson.com"
set "STATE=%USERPROFILE%\.claude\skills\weekly-work-log\.local-state"
set "LOG=%STATE%\weekly.log"
if not exist "%STATE%" mkdir "%STATE%"
cd /d "%REPO%" || (echo [%DATE% %TIME%] repo not found: %REPO% >> "%LOG%" & exit /b 1)
echo ==== weekly-work-log run %DATE% %TIME% ==== >> "%LOG%"
node scripts\draft-work-log-from-handoffs.mjs >> "%LOG%" 2>&1
node scripts\work-log-weekly.mjs >> "%LOG%" 2>&1
echo ==== done %DATE% %TIME% ==== >> "%LOG%"
endlocal
