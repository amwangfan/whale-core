@echo off
rem 鲸鱼 Whale —— 双击启动。第一次运行会自动准备 Python 环境（约 1–3 分钟）。
chcp 65001 >nul
title 鲸鱼 Whale（关闭此窗口即停止）
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo.
echo   第一次运行：正在准备环境，请稍等……
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if errorlevel 1 goto nopython
  py -3 -m venv .venv
) else (
  where python >nul 2>nul
  if errorlevel 1 goto nopython
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if errorlevel 1 goto nopython
  python -m venv .venv
)
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -e . -q
if errorlevel 1 goto failed

:run
".venv\Scripts\python.exe" -m whale %*
goto end

:nopython
echo.
echo   没有找到 Python 3.11 或更新的版本。
echo   请先安装 Python（https://www.python.org/downloads/ ，安装时勾选 "Add python.exe to PATH"），
echo   或者在 PowerShell 里运行一键安装：
echo     irm https://raw.githubusercontent.com/amwangfan/whale-core/main/scripts/install.ps1 ^| iex
pause
goto end

:failed
echo.
echo   环境准备失败。请检查网络后重新双击 Whale.cmd；
echo   如果仍然失败，删除本目录下的 .venv 文件夹再试一次。
pause

:end
