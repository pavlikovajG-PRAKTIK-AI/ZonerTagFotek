@echo off
rem WildSort - spusteni poklepanim. Nastartuje server a otevre prohlizec.
cd /d "%~dp0"
set "PATH=%LOCALAPPDATA%\Programs\ExifTool;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" run.py
pause
