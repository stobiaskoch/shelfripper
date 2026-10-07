@echo off
rem Liest den in der Weboberflaeche eingestellten Archivordner (config\archive_path.txt)
rem und uebergibt ihn als ARCHIVE_DIR an docker compose. Ohne Eintrag gilt der Ordner "archiv".
set "ARCHIVE_DIR="
if exist "%~dp0config\archive_path.txt" (
    chcp 65001 >nul
    set /p ARCHIVE_DIR=<"%~dp0config\archive_path.txt"
)
