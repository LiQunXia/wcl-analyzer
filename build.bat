@echo off
rem 在 Windows 上本地打包出 dist\WCL-Analyzer.exe
setlocal
cd /d "%~dp0"

where python >nul 2>nul || (
    echo 没有找到 python，请先安装 Python 3.12 并勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)

if not exist .venv-build (
    python -m venv .venv-build || goto :fail
)

.venv-build\Scripts\python -m pip install -q -r requirements.txt "pyinstaller>=6.10" || goto :fail
.venv-build\Scripts\python -m PyInstaller --noconfirm wcl.spec || goto :fail

echo.
echo 打包完成：dist\WCL-Analyzer.exe（把这一个文件发给别人即可）
pause
exit /b 0

:fail
echo.
echo 打包失败，请查看上面的报错。
pause
exit /b 1
