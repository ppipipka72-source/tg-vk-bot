@echo off
chcp 65001 >nul
cd /d "%~dp0"
title TG-VK bot (local test)

echo ============================================
echo   TG-VK bot - ЛОКАЛЬНЫЙ ЗАПУСК ДЛЯ ТЕСТОВ
echo ============================================
echo   Папка : %cd%
echo   Стоп  : Ctrl+C
echo --------------------------------------------
echo.

".venv\Scripts\python.exe" -m bridge.main

echo.
echo --------------------------------------------
echo   Бот остановлен (код выхода %errorlevel%)
echo --------------------------------------------
pause
