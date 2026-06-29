@echo off
:: ============================================================
::  Start Test Station Web Server
::  - Adds Windows Firewall rule for port 8000 (requires admin)
::  - Shows local network URL
::  - Launches web_server.py
:: ============================================================

:: Check if running as administrator
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: Change to the script's directory
cd /d "%~dp0"

:: ── Add Windows Firewall rule if not already there ──────────
netsh advfirewall firewall show rule name="TestStation-8000" >nul 2>&1
if %errorLevel% neq 0 (
    echo [+] Adding firewall rule for port 8000...
    netsh advfirewall firewall add rule ^
        name="TestStation-8000" ^
        dir=in ^
        action=allow ^
        protocol=TCP ^
        localport=8000 ^
        profile=any ^
        description="Test Station web server"
    echo [+] Firewall rule added.
) else (
    echo [=] Firewall rule already exists.
)

:: ── Show local IP ────────────────────────────────────────────
echo.
echo ============================================================
echo  Network URLs (use one of these from other devices):
echo ============================================================
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    set ip=%%a
    setlocal enabledelayedexpansion
    set ip=!ip: =!
    echo   http://!ip!:8000
    endlocal
)
echo   http://localhost:8000  (this machine only)
echo ============================================================
echo.

:: ── Start server ─────────────────────────────────────────────
echo [+] Starting server... (Ctrl+C to stop)
echo.
python web_server.py

pause
