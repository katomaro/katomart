@echo off
SETLOCAL ENABLEDELAYEDEXPANSION

if not exist "%~dp0.venv\Scripts\activate.bat" (
    echo .venv nao encontrado. Execute run.bat primeiro para configurar o ambiente.
    pause
    exit /b 1
)

call "%~dp0.venv\Scripts\activate.bat"

set "SETTINGS=%~dp0settings.json"
set "DL_PATH=./downloads"

if exist "%SETTINGS%" (
    for /f "tokens=*" %%A in ('python -c "import json; s=json.load(open(r'%SETTINGS%','r',encoding='utf-8')); print(s.get('download_path','./downloads'))"') do (
        set "DL_PATH=%%A"
    )
)

set "DB=%DL_PATH%\katomart_history.db"

if not exist "%DB%" (
    echo Banco de dados do historico nao encontrado em: %DB%
    echo Faca um download com o historico habilitado primeiro.
    pause
    exit /b 1
)

echo Abrindo dashboard em http://127.0.0.1:6102 (porta padrao, voce pode ter alterado)
python -m src.web.app --db "%DB%" --settings "%SETTINGS%" --port 6102

ENDLOCAL
