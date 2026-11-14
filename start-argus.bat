@echo off
rem ARGUS - one-click setup and start for Windows.
rem First run creates .venv and installs everything; later runs just start.
cd /d "%~dp0"

if not exist .venv (
    echo [ARGUS] Vytvaram virtualne prostredie...
    py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv
    if errorlevel 1 (
        echo [ARGUS] Python 3.12+ nie je nainstalovany. Stiahni ho z python.org
        pause
        exit /b 1
    )
    echo [ARGUS] Instalujem zavislosti ^(prvykrat to trva par minut^)...
    .venv\Scripts\python -m pip install --upgrade pip >nul
    .venv\Scripts\pip install -e ".[dev,desktop]"
    if errorlevel 1 (
        echo [ARGUS] Instalacia zlyhala, pozri chybu vyssie.
        pause
        exit /b 1
    )
)

if "%1"=="test" (
    .venv\Scripts\python -m pytest -q
    pause
    exit /b
)
if "%1"=="engine" (
    echo [ARGUS] Engine bezi, terminal: http://127.0.0.1:7421   ^(Ctrl+C = stop^)
    .venv\Scripts\python -m argus
    exit /b
)

rem default: desktop window with tray icon (starts the engine itself)
start "" .venv\Scripts\pythonw desktop\argus_shell.py
