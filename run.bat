@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TIMESTAMP=%%i"
set "LOGFILE=logs\run_%TIMESTAMP%.log"

echo.
echo ============================================================
echo  Free Token API Collector
echo  清除失效 Key -^> 采集验证 -^> 写入 cc-switch
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+
    if not defined FREE_TOKEN_NONINTERACTIVE pause
    exit /b 1
)

if exist "requirements.txt" (
    echo [INFO] Checking dependencies...
    pip install -r requirements.txt --quiet
)

echo [START]
powershell -NoProfile -Command "Write-Host (Get-Date -f 'yyyy-MM-dd HH:mm:ss')"
echo.

if "%~1"=="" (
    echo [STEP 1/2] Purging expired API keys from cc-switch...
    python main.py --purge 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOGFILE%'"
    echo.
    echo [STEP 2/2] Collecting and adding useful providers...
    python main.py 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOGFILE%' -Append"
) else (
    python main.py %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOGFILE%'"
)

echo.
echo [DONE] Log saved to %LOGFILE%
echo.
if not defined FREE_TOKEN_NONINTERACTIVE pause
