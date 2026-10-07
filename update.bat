@echo off
cd /d "%~dp0"
call "%~dp0archiv-pfad.bat"
echo Baue beide Container neu (Ripper und Weboberflaeche).
echo Bitte nur ausfuehren, wenn gerade keine CD gerippt wird.
echo.
pause
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo Fehler beim Bauen oder Starten. Bitte die Meldungen oben pruefen.
    pause
    exit /b 1
)
echo.
docker compose ps
echo.
echo Fertig. Weboberflaeche: http://localhost:8080
pause
