@echo off
REM Builds Subzeo.exe (PyInstaller) and, if Inno Setup is installed, Subzeo-Setup.exe
echo === Subzeo build ===
python -m pip install -r requirements.txt pyinstaller || goto :err
pyinstaller --noconfirm --clean --windowed --name Subzeo --icon assets\icon.ico ^
  --add-data "assets;assets" --collect-all yt_dlp subzeo.py || goto :err
echo.
echo App folder: dist\Subzeo   (run dist\Subzeo\Subzeo.exe)
where iscc >nul 2>nul
if %errorlevel%==0 (
  iscc installer.iss && echo Installer: Output\Subzeo-Setup.exe
) else (
  echo Install Inno Setup 6 from jrsoftware.org, then run: iscc installer.iss
)
goto :eof
:err
echo Build failed.
exit /b 1
