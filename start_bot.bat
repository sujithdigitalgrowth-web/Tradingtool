@echo off
echo Starting Trading Bot...
start "Dashboard" python dashboard.py
timeout /t 2 >nul
start "Bot" python bot.py
echo.
echo Bot and Dashboard started!
echo Dashboard: http://localhost:5000
pause
