@echo off
chcp 65001 >nul
setlocal

set VENV_DIR=.venv
set APP=radio.py
set REQ=requirements.txt
set SENTINEL=.venv\.installed

:: ── Создаём venv если нет ──────────────────────────────────
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [setup] Создаю виртуальное окружение...
    python -m venv %VENV_DIR%
    if errorlevel 1 (
        echo [ошибка] python не найден или venv не создался.
        pause & exit /b 1
    )
)

:: ── Активируем venv ────────────────────────────────────────
call "%VENV_DIR%\Scripts\activate.bat"

:: ── Ставим зависимости только если нет sentinel ────────────
if not exist "%SENTINEL%" (
    echo [setup] Устанавливаю зависимости...
    pip install -r %REQ% --quiet
    if errorlevel 1 (
        echo [ошибка] Не удалось установить зависимости.
        pause & exit /b 1
    )
    echo. > "%SENTINEL%"
    echo [setup] Зависимости установлены.
) else (
    echo [setup] Зависимости актуальны, пропускаю установку.
)

:: ── Запуск ─────────────────────────────────────────────────
echo.
echo  Запускаю %APP%...
echo.
python %APP%
pause