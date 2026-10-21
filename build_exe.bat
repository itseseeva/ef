@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title EZF - сборка exe

rem ===== EZF: сборка EZF.exe одной кнопкой (2026-10-11) =====
rem 1) проверка окружения  2) PyInstaller  3) сборка  4) настройки в llm.env  5) самопроверка exe  6) EZF.zip для друга
rem Результат: release\EZF.zip - один файл для друга (внутри EZF.exe, llm.env и скрытая _internal).
rem Себе - папка release\EZF. dist\ - это фронт, его не трогаем.

rem Python, в котором стоят пакеты программы: .venv, если в нём всё есть, иначе системный python
rem (программу могли запускать системным python, а .venv остался пустым).
set "PY=python"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import webview, easyocr, requests, cv2" >nul 2>&1 && set "PY=.venv\Scripts\python.exe"
)
echo Python для сборки: %PY%

echo.
echo [1/6] Проверяю окружение...
"%PY%" tools\build_check.py
if errorlevel 1 goto :fail

echo.
echo [2/6] Ставлю/обновляю PyInstaller...
"%PY%" -m pip install -q -U "pyinstaller>=6.10" pyinstaller-hooks-contrib
if errorlevel 1 goto :fail

echo.
echo [3/6] Собираю EZF.exe (5-15 минут, torch большой - это нормально)...
rem Цепочки скиллов, сохранённые в прошлом exe, - в проект (иначе новый exe вышел бы со старыми).
"%PY%" tools\pack_release.py config
if exist "release\EZF" rmdir /s /q "release\EZF"
"%PY%" -m PyInstaller --noconfirm --clean --distpath release --workpath build_exe_tmp EZF.spec
if errorlevel 1 goto :fail

echo.
echo [4/6] Настройки рядом с exe: .env + llm.env -> один llm.env...
"%PY%" tools\pack_release.py env
if errorlevel 1 goto :fail

echo.
echo [5/6] Самопроверка собранного exe...
rem exe собран без консоли: cmd не ждёт такие программы сам - start /wait ждёт и отдаёт код выхода.
rem Результат самопроверки exe пишет в selftest.txt - показываем его здесь.
if exist "release\EZF\selftest.txt" del /q "release\EZF\selftest.txt"
start "" /wait "release\EZF\EZF.exe" --selftest
set "ST=%errorlevel%"
if exist "release\EZF\selftest.txt" (type "release\EZF\selftest.txt") else (
    echo    selftest.txt не появился - exe упал при запуске. Лог:
    if exist "release\EZF\_internal\EZF_log.txt" type "release\EZF\_internal\EZF_log.txt"
    goto :fail
)
if not "%ST%"=="0" goto :fail

echo.
echo [6/6] Пакую архив для друга (1-3 минуты)...
if exist "release\EZF.zip" del /q "release\EZF.zip"
"%PY%" tools\pack_release.py zip
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  ГОТОВО: release\EZF.zip  - этот один файл и отправляй другу.
echo  Друг: распаковать архив (правой кнопкой - Извлечь все),
echo        открыть папку EZF и запустить EZF.exe.
echo  Запускать прямо из архива нельзя - без распаковки exe не стартует.
echo  Себе: release\EZF\EZF.exe работает как раньше.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ============================================================
echo  ОШИБКА. Пришли этот текст (и release\EZF\_internal\EZF_log.txt, если есть) - разберу.
echo ============================================================
pause
exit /b 1
