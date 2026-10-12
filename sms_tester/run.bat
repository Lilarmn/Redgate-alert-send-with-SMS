@echo off
chcp 65001 >nul
title سامانه تست و عیب‌یابی اتصال به سرور پیامک (SMS Gateway Tester)

cd /d "%~dp0"

echo ======================================================================
echo           📡 سامانه تست و ارزیابی اتصال به سرور پیامک
echo           بر اساس سند راهنمای رسمی API ارسال پیامک (نسخه ۱.۰)
echo ======================================================================
echo.

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] پایتون روی سیستم یافت نشد! لطفاً از نصب بودن Python اطمینان حاصل کنید.
    pause
    exit /b 1
)

echo [INFO] در حال راه‌اندازی داشبورد تحت وب روی پورت 8080...
echo [INFO] آدرس پنل کاربری: http://127.0.0.1:8080
echo [INFO] برای توقف برنامه کلیدهای Ctrl+C را فشار دهید.
echo.

:: Open default browser after 2 seconds in background
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8080"

:: Start the Python Flask app
python app.py

pause
