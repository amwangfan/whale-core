# 鲸鱼 Whale 一键安装（Windows）
#   在 PowerShell 中运行：
#     irm https://raw.githubusercontent.com/amwangfan/whale-core/main/scripts/install.ps1 | iex
#   再次运行即升级到最新版本；数据（记忆、设置）保存在 %USERPROFILE%\.whale，升级不会丢失。
param([string]$Ref = "main", [switch]$NoLaunch)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$AppDir = Join-Path $env:LOCALAPPDATA "Whale"
$Venv = Join-Path $AppDir "venv"
$Source = "https://github.com/amwangfan/whale-core/archive/refs/heads/$Ref.zip"

function Say($text) { Write-Host "  $text" -ForegroundColor Cyan }

function Find-Python {
    # returns @{exe=...; args=@(...)} for the first Python 3.11+ found
    $candidates = @(@{exe = "py"; args = @("-3.13")}, @{exe = "py"; args = @("-3.12")},
                    @{exe = "py"; args = @("-3.11")}, @{exe = "python"; args = @()})
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ok = & $c.exe @($c.args + @("-c", "import sys; print(sys.version_info >= (3, 11))")) 2>$null
            if ($ok -eq "True") { return $c }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "  🐋 安装鲸鱼 Whale" -ForegroundColor White
$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "没有找到 Python 3.11+，正在通过 winget 安装 Python 3.12……"
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements | Out-Null
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        $py = Find-Python
    }
    if (-not $py) {
        Write-Host "  需要 Python 3.11 或更新版本。请从 https://www.python.org/downloads/ 安装（勾选 Add python.exe to PATH）后重新运行本命令。" -ForegroundColor Yellow
        exit 1
    }
}

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Say "创建运行环境……"
    & $py.exe @($py.args + @("-m", "venv", $Venv))
}
$vpy = Join-Path $Venv "Scripts\python.exe"
Say "下载并安装最新版本（约 1–3 分钟）……"
& $vpy -m pip install --upgrade pip -q
$already = & $vpy -m pip show whale-core 2>$null
if ($already) {
    & $vpy -m pip install -q --upgrade --force-reinstall "whale-core @ $Source"
} else {
    & $vpy -m pip install -q "whale-core @ $Source"
}
if ($LASTEXITCODE -ne 0) { Write-Host "  安装失败，请检查网络后重试。" -ForegroundColor Yellow; exit 1 }

$Launcher = Join-Path $AppDir "Whale.cmd"
@"
@echo off
chcp 65001 >nul
title 鲸鱼 Whale（关闭此窗口即停止）
"$vpy" -m whale %*
"@ | Set-Content -Path $Launcher -Encoding UTF8

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "鲸鱼 Whale.lnk"))
    $lnk.TargetPath = $Launcher
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description = "鲸鱼 Whale —— 你的私人 AI"
    $lnk.Save()
}

Write-Host ""
Say "安装完成！桌面和开始菜单里有「鲸鱼 Whale」。"
Say "双击它即可启动，浏览器会自动打开控制台；关闭黑色窗口即停止。"
Write-Host ""
if (-not $NoLaunch) { Start-Process $Launcher }
