@echo off
REM ============================================================
REM  start-water-inspection.bat
REM  1) Makes sure usbipd-win is installed (installs it if missing)
REM  2) Finds the USEEPLUS camera (VID:PID = 2ce3:3828) and binds it
REM  3) Attaches the device to WSL2 so Docker Desktop can see it
REM  4) Starts the container with docker compose up -d
REM
REM  Must be placed next to docker-compose.yml + .env + data\
REM ============================================================

setlocal
set VIDPID=2ce3:3828
set PROJECT_DIR=%~dp0

REM ---- 1) Needs Administrator rights (usbipd bind/attach require it) ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Administrator rights required - relaunching...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================================
echo   Water Inspection System - Startup
echo ============================================================
echo.

REM ---- 2) Make sure usbipd is installed ----
where usbipd >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] usbipd-win is not installed - installing now...
    winget install --exact --silent dorssel.usbipd-win
    echo [*] If this was the first install, you may need to reboot
    echo     or run this file again.
    pause
    exit /b 1
)

REM ---- 3) Find the camera's busid (VID:PID = 2ce3:3828) ----
echo [1/4] Looking for the USEEPLUS camera (%VIDPID%)...
set CAM_BUSID=
for /f "tokens=1" %%A in ('usbipd list ^| findstr /i "%VIDPID%"') do (
    if not defined CAM_BUSID set CAM_BUSID=%%A
)

if not defined CAM_BUSID (
    echo [X] Camera not found. Make sure it is plugged into USB.
    echo.
    usbipd list
    pause
    exit /b 1
)
echo     Found on busid: %CAM_BUSID%
echo.

REM ---- 4) Share the device (bind) - only needed once, safe to repeat ----
echo [2/4] Binding the device...
usbipd bind --busid=%CAM_BUSID% --force >nul 2>&1
echo.

REM ---- 5) Attach the device to WSL2 (Docker Desktop must be running) ----
echo [3/4] Attaching to WSL2...
usbipd attach --wsl --busid=%CAM_BUSID%
if %errorlevel% neq 0 (
    echo [X] Attach failed - make sure Docker Desktop and WSL are running.
    pause
    exit /b 1
)
echo.

REM ---- 6) Start the container ----
echo [4/4] Starting the container (docker compose up -d)...
cd /d "%PROJECT_DIR%"
docker compose up -d

echo.
echo ============================================================
echo   Done. Follow logs with:  docker compose logs -f
echo   If the camera is unplugged/replugged, run this file again
echo   so usbipd can re-attach it.
echo ============================================================
pause
