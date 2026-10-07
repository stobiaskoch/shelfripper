@echo off
cd /d "%~dp0"
call "%~dp0archiv-pfad.bat"
echo Baue und starte die Weboberflaeche (der Ripper laeuft dabei ungestoert weiter) ...
docker compose up -d --build web
if errorlevel 1 (
    echo.
    echo Fehler beim Starten. Bitte die Meldungen oben pruefen.
    pause
    exit /b 1
)
timeout /t 3 /nobreak >nul
start "" http://localhost:8080
