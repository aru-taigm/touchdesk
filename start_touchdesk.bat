@echo off
rem TouchDesk を起動する。スマホ接続中だけ解像度を下げ、起動時にスマホ用の QR コードを表示する。
rem 解像度を変えたくないときは --no-resize、QR を出したくないときは --no-qr、本物のタッチを使わないときは --no-touch を末尾に付ける。
cd /d "%~dp0"
set WEB=C:\noVNC
if exist "%~dp0noVNC\vnc.html" set WEB=%~dp0noVNC
python -m pip install -q websockify segno
python touchdesk.py --size 1280x720 --web "%WEB%"
pause
