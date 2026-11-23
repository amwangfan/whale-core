# Whale-Core 本地开发启动（Windows PowerShell）
#   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1            # 安装依赖、跑测试、启动
#   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -NoTest    # 跳过测试
param([switch]$NoTest, [int]$Port = 8080, [string]$Data = "$PSScriptRoot\..\.whale-dev")

$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."

if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip -q
& $py -m pip install -e ".[dev]" -q

if (-not $NoTest) {
    & $py -m pytest -q
    if ($LASTEXITCODE -ne 0) { Write-Host "测试未通过，已停止。" -ForegroundColor Red; exit 1 }
}

& $py -m whale --data $Data serve --port $Port
