@echo off
rem CMD/PowerShell 快捷入口: px curl -sI https://www.google.com
where python >nul 2>nul
if %errorlevel%==0 (
  python -u "%~dp0flclash_proxy.py" %*
) else (
  py -3 -u "%~dp0flclash_proxy.py" %*
)
