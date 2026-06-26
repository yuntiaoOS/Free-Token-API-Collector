# ============================================================
# Free Token API Collector — 一键运行脚本
# 双击或在 PowerShell 中执行: .\run.ps1
# ============================================================

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# Ensure logs directory exists
if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" | Out-Null }

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = "logs\run_${timestamp}.log"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Free Token API Collector" -ForegroundColor Cyan
Write-Host " 清除失效 Key -> 采集验证 -> 写入 cc-switch" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[ERROR] Python not found. Please install Python 3.10+" -ForegroundColor Red
    if (-not $env:FREE_TOKEN_NONINTERACTIVE) {
        Read-Host "Press Enter to exit"
    }
    exit 1
}

# Install dependencies
$reqFile = Join-Path $scriptDir "requirements.txt"
if (Test-Path $reqFile) {
    Write-Host "[INFO] Checking dependencies..." -ForegroundColor Yellow
    pip install -r $reqFile --quiet
}

# Run purge then collect (or pass through custom args)
Write-Host "[START] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
Write-Host ""

if ($args.Count -eq 0) {
    Write-Host "[STEP 1/2] Purging expired API keys from cc-switch..." -ForegroundColor Yellow
    python main.py --purge 2>&1 | Tee-Object -FilePath $logFile
    Write-Host ""
    Write-Host "[STEP 2/2] Collecting and adding useful providers..." -ForegroundColor Yellow
    python main.py 2>&1 | Tee-Object -FilePath $logFile -Append
} else {
    python main.py @args 2>&1 | Tee-Object -FilePath $logFile
}

Write-Host ""
Write-Host "[DONE] Log saved to $logFile" -ForegroundColor Green
Write-Host ""
if (-not $env:FREE_TOKEN_NONINTERACTIVE) {
    Read-Host "Press Enter to exit"
}
