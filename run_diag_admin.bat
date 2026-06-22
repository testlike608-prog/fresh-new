@echo off
:: Self-elevate to Administrator
net session >nul 2>&1
if %errorLevel% NEQ 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process cmd.exe -ArgumentList '/k cd /d \"C:\Users\Hisham Elbadry\Desktop\Fresh\" && \"C:\Users\Hisham Elbadry\AppData\Local\Programs\Python\Python312\python.exe\" test_camera_diag.py && pause' -Verb RunAs"
    exit
)
cd /d "C:\Users\Hisham Elbadry\Desktop\Fresh"
"C:\Users\Hisham Elbadry\AppData\Local\Programs\Python\Python312\python.exe" test_camera_diag.py
pause
