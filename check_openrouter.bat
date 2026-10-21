@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Проверка не требует пакетов: подойдёт любой Python (сначала .venv, иначе системный).
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" tools\openrouter_check.py
