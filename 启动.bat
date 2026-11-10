@echo off
chcp 936 >nul
setlocal
title 今日校园自动签到 - 管理台

cd /d "%~dp0"

echo ============================================================
echo    今日校园自动签到 - Web 管理台
echo ============================================================
echo.

rem ---------- 环境检查 ----------
if not exist "web-server\.venv\Scripts\python.exe" (
  echo [错误] 还没安装运行环境。
  echo.
  echo   请先双击运行 "一键安装.bat"
  echo.
  pause
  exit /b 1
)
if not exist "jrxy-engine\.venv\Scripts\python.exe" (
  echo [错误] 引擎环境缺失。
  echo.
  echo   请先双击运行 "一键安装.bat"
  echo.
  pause
  exit /b 1
)

rem ---------- 端口占用检查 ----------
set "PORT=8000"
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [警告] 端口 %PORT% 已被占用。
  echo.
  echo   可能已经有一个服务在运行了。
  echo   请先在浏览器打开 http://127.0.0.1:%PORT% 看看。
  echo   如果确认要重启，请关掉之前的窗口或结束占用进程。
  echo.
  choice /c YN /m "是否继续启动（可能失败）"
  if errorlevel 2 exit /b 0
)

echo   访问地址: http://127.0.0.1:8000
echo   定时签到: 每天 22:05
echo   关闭本窗口即停止服务
echo.
echo   正在启动，就绪后会自动打开浏览器...
echo.

rem 后台等待端口就绪，然后打开浏览器（最长 40 秒）
start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "$u='http://127.0.0.1:8000/api/status'; for($i=0;$i -lt 80;$i++){ try{ Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 1 | Out-Null; Start-Process 'http://127.0.0.1:8000'; break }catch{ Start-Sleep -Milliseconds 500 } }"

cd /d "%~dp0web-server"
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000

echo.
echo ============================================================
echo   服务已停止
echo ============================================================
pause
