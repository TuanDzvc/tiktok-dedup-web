@echo off
chcp 65001 >nul
title TikTok Dedup Studio Web Server
echo ======================================================================
echo          TIKTOK DEDUP STUDIO - HỆ THỐNG CHECK TRÙNG WEB
echo ======================================================================
echo.
cd /d "%~dp0"
echo [*] Đang khởi động máy chủ Web và kết nối tên miền Cloudflare miễn phí...
echo.
python server.py
pause
