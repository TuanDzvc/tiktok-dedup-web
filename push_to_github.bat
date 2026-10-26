@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo =======================================================
echo     DANG DAY CODE LEN GITHUB: TuanDzvc/tiktok-dedup-web
echo =======================================================
echo.
git push -u origin main
echo.
echo =======================================================
echo     HOAN TAT!
echo =======================================================
pause
