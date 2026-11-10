@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion
title jrxy-auto-sign 一键安装

cd /d "%~dp0"

echo ============================================================
echo    今日校园自动签到 - 环境安装
echo ============================================================
echo.

rem ---------- 1. 找 Python 3.10 ----------
set "PY="
py -3.10 --version >nul 2>&1 && set "PY=py -3.10"
if not defined PY (
  py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
)
if not defined PY (
  py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
)
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo [错误] 没有找到 Python。
  echo.
  echo   请先从下面地址安装 Python 3.10（推荐）：
  echo     https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe
  echo.
  echo   安装时请务必勾选 "Add Python 3.10 to PATH"。
  echo.
  pause
  exit /b 1
)

echo [检查] 使用的 Python：
%PY% --version
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 (
  echo.
  echo [错误] Python 版本过低，需要 3.10 或更高。
  pause
  exit /b 1
)
echo.

set "PIP_INDEX=-i https://pypi.tuna.tsinghua.edu.cn/simple"

rem ---------- 2. 引擎环境 ----------
echo [1/3] 创建引擎虚拟环境 jrxy-engine\.venv ...
if not exist "jrxy-engine\.venv\Scripts\python.exe" (
  %PY% -m venv "jrxy-engine\.venv"
  if errorlevel 1 goto :fail
) else (
  echo       已存在，跳过创建
)
echo       升级 pip ...
"jrxy-engine\.venv\Scripts\python.exe" -m pip install %PIP_INDEX% --upgrade pip >nul 2>&1

echo       安装引擎依赖（约 1-3 分钟，请耐心等待）...
"jrxy-engine\.venv\Scripts\python.exe" -m pip install %PIP_INDEX% -r "jrxy-engine\requirements.txt"
if errorlevel 1 goto :fail
echo       引擎依赖完成。
echo.

rem ---------- 3. Web 环境 ----------
echo [2/3] 创建 Web 虚拟环境 web-server\.venv ...
if not exist "web-server\.venv\Scripts\python.exe" (
  %PY% -m venv "web-server\.venv"
  if errorlevel 1 goto :fail
) else (
  echo       已存在，跳过创建
)
echo       升级 pip ...
"web-server\.venv\Scripts\python.exe" -m pip install %PIP_INDEX% --upgrade pip >nul 2>&1

echo       安装 Web 依赖 ...
"web-server\.venv\Scripts\python.exe" -m pip install %PIP_INDEX% -r "web-server\requirements.txt"
if errorlevel 1 goto :fail
echo       Web 依赖完成。
echo.

rem ---------- 4. 自检 ----------
echo [3/3] 自检 ...
"jrxy-engine\.venv\Scripts\python.exe" -W ignore -c "import yaml,requests,Crypto,rsa,pyDes,bs4,requests_toolbelt,apprise" 2>nul
if errorlevel 1 (
  echo       [警告] 引擎依赖自检未通过，请查看上面的错误信息
) else (
  echo       引擎依赖 OK
)
"web-server\.venv\Scripts\python.exe" -c "import fastapi,uvicorn,yaml" 2>nul
if errorlevel 1 (
  echo       [警告] Web 依赖自检未通过
) else (
  echo       Web 依赖 OK
)
echo.

echo ============================================================
echo    安装完成！
echo ============================================================
echo.
echo   下一步：双击 "启动.bat" 即可使用
echo   浏览器会自动打开 http://127.0.0.1:8000
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo   [安装失败]
echo ============================================================
echo   常见原因：
echo     1. 网络问题 - 可尝试换镜像源（用记事本编辑本文件里的 PIP_INDEX）
echo     2. Python 版本过低 - 需要 3.10+
echo     3. 缺少 VC++ 运行库 - 安装 vc_redist.x64.exe
echo.
pause
exit /b 1
