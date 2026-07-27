@echo off
start "" cmd /k "cd /d D:\Users\William\ARIA-Lite Development\ARIA-Lite && python backend\ws_server.py"
start "" cmd /k "cd /d D:\Users\William\ARIA-Lite Development\ARIA-Lite\webui && python secure_server.py"

echo(
echo ================================================
echo   Welcome to ARIA Lite
echo(
echo   * Backend and WebUI are launching
echo   * Please wait a few seconds
echo   * If a window closes instantly, check Python path
echo(
echo   Enjoy your ARIA Lite experience!
echo ================================================
echo(

timeout /t 20 >nul
exit
