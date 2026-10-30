@echo off
cd /d "%~dp0"
echo [1/3] Installing packages...
pip install -r requirements.txt pyinstaller || goto :err
echo [2/3] Creating icon...
python make_icon.py || goto :err
echo [3/3] Building exe (takes a few minutes)...
pyinstaller --noconfirm --clean --onefile --noconsole --name LoLRemote --icon icon.ico ^
  --add-data "index.html;." ^
  --collect-submodules uvicorn --collect-submodules websockets ^
  --hidden-import pystray._win32 ^
  lol_agent.py || goto :err
echo.
echo Done: dist\LoLRemote.exe
pause
exit /b 0
:err
echo.
echo Build failed. Check the messages above.
pause
exit /b 1
