@echo off
chcp 65001 >nul
cd /d "%~dp0"
title TG-VK bot - push to GitHub

echo ============================================
echo   ОТПРАВКА ИЗМЕНЕНИЙ НА GITHUB
echo ============================================
echo.

git add -A

echo --- Что изменилось: ---
git status --short
echo.

set "msg="
set /p "msg=Сообщение коммита (пусто + Enter = отмена): "

if "%msg%"=="" (
  echo.
  echo Отменено, ничего не отправлено.
  pause
  exit /b
)

git commit -m "%msg%"
if errorlevel 1 (
  echo.
  echo Коммитить нечего или ошибка коммита.
  pause
  exit /b
)

git push
echo.
echo --------------------------------------------
echo   Готово. Не забудь на сервере:
echo     cd /opt/tg-vk-bot ^&^& git pull ^&^& systemctl restart tgvk-bot
echo --------------------------------------------
pause
