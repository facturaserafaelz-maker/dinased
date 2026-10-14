@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv" (
  echo Creando el entorno de Python por primera vez...
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo DINASED iniciando en este equipo...
echo Abra en el navegador: http://127.0.0.1:8080
echo Para acceder desde celulares y usar GPS se debe configurar HTTPS. Consulte README.txt.
echo Para detener, cierre esta ventana.
python -m waitress --listen=0.0.0.0:8080 app:app
pause
