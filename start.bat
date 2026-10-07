@echo off
setlocal
cd /d "%~dp0"
call "%~dp0archiv-pfad.bat"

rem Administratorrechte anfordern, falls noetig
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set WSL_UTF8=1
set "LOG=%~dp0start-log.txt"
echo === %date% %time% === > "%LOG%"

echo.
echo Angeschlossene USB-Geraete:
echo.
usbipd list
echo --- usbipd list --- >> "%LOG%"
usbipd list >> "%LOG%" 2>&1

echo.
set /p BUSID=BUSID des DVD-Laufwerks eingeben (z.B. 2-3): 
echo BUSID=%BUSID% >> "%LOG%"

echo Reiche Laufwerk an WSL2 durch ...
echo --- usbipd bind --- >> "%LOG%"
usbipd bind --busid %BUSID% >> "%LOG%" 2>&1
echo --- usbipd attach --- >> "%LOG%"
usbipd attach --wsl --busid %BUSID% >> "%LOG%" 2>&1
timeout /t 5 /nobreak >nul

echo Pruefe /dev/sr0 ...
echo --- /dev/sr0 --- >> "%LOG%"
wsl -d docker-desktop ls -l /dev/sr0 >> "%LOG%" 2>&1
if errorlevel 1 (
    echo --- modprobe usb-storage --- >> "%LOG%"
    wsl -u root modprobe usb-storage >> "%LOG%" 2>&1
    timeout /t 5 /nobreak >nul
    wsl -d docker-desktop ls -l /dev/sr0 >> "%LOG%" 2>&1
)

echo Baue und starte Container (kann beim ersten Mal einige Minuten dauern) ...
echo --- docker compose up --- >> "%LOG%"
docker compose up -d --build >> "%LOG%" 2>&1
timeout /t 8 /nobreak >nul
echo --- docker logs --- >> "%LOG%"
docker logs cdripper >> "%LOG%" 2>&1

echo.
echo Fertig. Protokoll: %LOG%
echo.
docker logs cdripper
echo.
pause
