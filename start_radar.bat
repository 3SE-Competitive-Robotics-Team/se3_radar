@echo off
rem 双击启动调试台：在 WSL 里运行 scripts/radar.py（启动全部板子），几秒后自动打开浏览器。
rem 关掉这个窗口或按 Ctrl-C 即停止全部板子。
chcp 65001 >nul
start "" /b cmd /c "timeout /t 4 /nobreak >nul && start "" http://127.0.0.1:8800"
wsl.exe --cd "%~dp0" python3 scripts/radar.py --no-browser %*
pause
