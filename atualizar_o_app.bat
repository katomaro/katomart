@echo off
TITLE Atualizar Katomart
CLS

python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    ECHO [ERRO] Python nao encontrado no PATH.
    ECHO Por favor, instale o Python 3.12+ e marque a opcao "Add Python to PATH".
    PAUSE
    EXIT /B
)

ECHO [1/2] Baixando e instalando nova versao...
python updater.py
IF %ERRORLEVEL% NEQ 0 (
    ECHO.
    ECHO [ERRO] Falha na atualizacao dos arquivos.
    PAUSE
    EXIT /B
)

ECHO.
ECHO ===================================================
ECHO           Atualizacao Concluida!
ECHO ===================================================
ECHO Pode fechar esta janela e iniciar o programa.
PAUSE