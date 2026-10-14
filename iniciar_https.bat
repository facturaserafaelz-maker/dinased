@echo off
chcp 65001 >nul
cd /d "%~dp0"
call .venv\Scripts\activate
set DINASED_CERT=%~dp0cert.pem
set DINASED_KEY=%~dp0key.pem
set DINASED_HTTPS=1
set DINASED_PUBLIC_URL=https://192.168.100.148:8080
echo Abra en el navegador: https://192.168.100.148:8080
python app.py
pause