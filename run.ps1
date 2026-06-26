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
Write-Host " 采集免费AI Token -> 验证可用性 -> 写入cc-switch" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[ERROR] Python not found. Please install Python 3.10+" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Check yaml module
$yamlCheck = python -c "import yaml" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[INFO] Installing PyYAML..." -ForegroundColor Yellow
    pip install pyyaml --quiet
}

# Run the collector
Write-Host "[START] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
Write-Host ""

python main.py 2>&1 | Tee-Object -FilePath $logFile

Write-Host ""
Write-Host "[DONE] Log saved to $logFile" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to exit"
