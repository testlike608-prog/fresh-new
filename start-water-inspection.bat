@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

:: ============================================================
::  start-water-inspection.bat
::  1) يتأكد إن usbipd-win متثبت (يثبته لو ناقص)
::  2) يلاقي كاميرا USEEPLUS (VID:PID = 2ce3:3828) ويعمل bind
::  3) يعمل attach للجهاز على WSL2 عشان Docker Desktop يشوفه
::  4) يشغّل الكونتينر بـ docker compose up -d
::
::  لازم يتحط جنب docker-compose.yml + .env + data/
:: ============================================================

set VIDPID=2ce3:3828
set PROJECT_DIR=%~dp0

:: ---- 1) لازم صلاحيات Administrator (usbipd bind/attach محتاجينها) ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] محتاج صلاحيات Administrator - هعيد التشغيل تلقائي...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================================
echo   Water Inspection System - Startup
echo ============================================================
echo.

:: ---- 2) تأكد إن usbipd متثبت ----
where usbipd >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] usbipd-win مش متثبت - بيتثبت دلوقتي...
    winget install --exact --silent dorssel.usbipd-win
    echo [*] لو ده أول تثبيت، ممكن تحتاج تعيد تشغيل الجهاز أو تفتح الـ .bat تاني.
    pause
    exit /b 1
)

:: ---- 3) دور على busid الكاميرا (VID:PID = 2ce3:3828) ----
echo [1/4] بدور على كاميرا USEEPLUS (%VIDPID%)...
set CAM_BUSID=
for /f "tokens=1,2" %%A in ('usbipd list ^| findstr /i "%VIDPID%"') do (
    if "!CAM_BUSID!"=="" set CAM_BUSID=%%A
)

if "%CAM_BUSID%"=="" (
    echo [X] مش لاقي الكاميرا متوصلة. اتأكد إنها موصولة بالـ USB.
    echo.
    usbipd list
    pause
    exit /b 1
)
echo     لقيتها على busid: %CAM_BUSID%
echo.

:: ---- 4) شيّر الجهاز (bind) - أول مرة بس، الباقي بيتخطى بأمان ----
echo [2/4] بعمل bind للجهاز...
usbipd bind --busid=%CAM_BUSID% --force >nul 2>&1
echo.

:: ---- 5) وصّل الجهاز بالـ WSL2 (لازم Docker Desktop شغال) ----
echo [3/4] بعمل attach للـ WSL2...
usbipd attach --wsl --busid=%CAM_BUSID%
if %errorlevel% neq 0 (
    echo [X] فشل الـ attach - اتأكد إن Docker Desktop + WSL شغالين وحاول تاني.
    pause
    exit /b 1
)
echo.

:: ---- 6) شغّل الكونتينر ----
echo [4/4] بشغّل الكونتينر (docker compose up -d)...
cd /d "%PROJECT_DIR%"
docker compose up -d

echo.
echo ============================================================
echo   تم التشغيل. للمتابعة:  docker compose logs -f
echo   لو الكاميرا وقعت (unplug/replug) لازم تشغّل الـ .bat ده تاني
echo   عشان usbipd يعمل attach من جديد.
echo ============================================================
pause
