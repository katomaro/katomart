@echo off
SETLOCAL ENABLEDELAYEDEXPANSION

echo Verificando se o ambiente virtual ".venv" existe
if not exist "%~dp0.venv\Scripts\activate.bat" (
    echo .venv nao encontrado. Tentando criar com Python 3.12
    py -3.12 -m venv "%~dp0.venv" 2>nul || (
        echo Falha ao criar venv com 'py -3.12'. Tentando 'python -m venv'
        python -m venv "%~dp0.venv" 2>nul || (
            echo Erro: Nao foi possivel criar o venv. Verifique se o Python 3.12 esta instalado e presente no PATH.
            pause
            exit /b 1
        )
    )
) else (
    echo .venv ja existe.
)

echo Ativando o ambiente virtual
call "%~dp0.venv\Scripts\activate.bat"
if ERRORLEVEL 1 (
    echo Erro ao ativar o venv.
    exit /b 1
)

if exist "%~dp0requirements.txt" (
    echo Instalando dependencias do requirements.txt
    pip install -r "%~dp0requirements.txt"
    if ERRORLEVEL 1 (
        echo Aviso: pip retornou erro durante a instalacao das dependencias, verirfique no grupo do telegram @GatosDodois.
    )
    echo Instalando o navegador Chromium pelo Playwright
    python -m playwright install chromium
    if ERRORLEVEL 1 (
        echo Aviso: houve um problema ao instalar o Chromium via Playwright.
    )
) else (
    echo Arquivo requirements.txt nao encontrado. Pulando instalacao.
)

echo Executando a aplicacao (python main.py)
python "%~dp0main.py" %*

ENDLOCAL
exit /b %ERRORLEVEL%
