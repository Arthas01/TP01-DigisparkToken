@echo off
setlocal
py -3.13 -m pip install --upgrade pip
if errorlevel 1 goto erro
py -3.13 -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto erro
echo.
echo Dependencias instaladas para Python 3.13.
echo Use tambem py -3.13 para executar o programa.
pause
exit /b 0
:erro
echo.
echo Nao foi possivel instalar. Confira se Python 3.13 esta instalado (py -0p).
pause
exit /b 1
