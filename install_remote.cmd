@echo off
setlocal

set "APP_NAME=Madrix Hue Bridge"
set "APP_DIR=%LocalAppData%\Programs\MadrixHueBridge"
set "START_MENU_DIR=%AppData%\Microsoft\Windows\Start Menu\Programs\Madrix Hue Bridge"
set "DESKTOP_SHORTCUT=%UserProfile%\Desktop\Madrix Hue Bridge.lnk"
set "EXE_NAME=MadrixHueBridgeUI.exe"

echo Installing %APP_NAME%...

if not exist "%APP_DIR%" mkdir "%APP_DIR%"
if not exist "%START_MENU_DIR%" mkdir "%START_MENU_DIR%"

copy /Y "%EXE_NAME%" "%APP_DIR%\%EXE_NAME%" >nul
copy /Y "config.example.json" "%APP_DIR%\config.example.json" >nul
copy /Y "README.md" "%APP_DIR%\README.md" >nul

if not exist "%APP_DIR%\config.json" (
    copy /Y "%APP_DIR%\config.example.json" "%APP_DIR%\config.json" >nul
)

(
echo @echo off
echo setlocal
echo taskkill /IM "%EXE_NAME%" /F ^>nul 2^>nul
echo rd /S /Q "%APP_DIR%"
echo del /Q "%START_MENU_DIR%\Madrix Hue Bridge.lnk" ^>nul 2^>nul
echo rd /Q "%START_MENU_DIR%" ^>nul 2^>nul
echo del /Q "%DESKTOP_SHORTCUT%" ^>nul 2^>nul
) > "%APP_DIR%\uninstall.cmd"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; " ^
  "$desktop = $ws.CreateShortcut('%DESKTOP_SHORTCUT%'); " ^
  "$desktop.TargetPath = Join-Path '%APP_DIR%' '%EXE_NAME%'; " ^
  "$desktop.WorkingDirectory = '%APP_DIR%'; " ^
  "$desktop.Arguments = '--config ""' + (Join-Path '%APP_DIR%' 'config.json') + '""'; " ^
  "$desktop.IconLocation = Join-Path '%APP_DIR%' '%EXE_NAME%'; " ^
  "$desktop.Save(); " ^
  "$menu = $ws.CreateShortcut((Join-Path '%START_MENU_DIR%' 'Madrix Hue Bridge.lnk')); " ^
  "$menu.TargetPath = Join-Path '%APP_DIR%' '%EXE_NAME%'; " ^
  "$menu.WorkingDirectory = '%APP_DIR%'; " ^
  "$menu.Arguments = '--config ""' + (Join-Path '%APP_DIR%' 'config.json') + '""'; " ^
  "$menu.IconLocation = Join-Path '%APP_DIR%' '%EXE_NAME%'; " ^
  "$menu.Save()"

start "" "%APP_DIR%\%EXE_NAME%" --config "%APP_DIR%\config.json"

echo.
echo Installation complete.
echo App folder: "%APP_DIR%"
exit /b 0
