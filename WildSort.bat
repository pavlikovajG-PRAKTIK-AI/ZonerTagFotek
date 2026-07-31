@echo off
rem WildSort - spusteni poklepanim. Nastartuje server a otevre prohlizec.
rem
rem Python se HLEDA, nezadava se pevnou cestou. Pevna cesta na jednu verzi
rem (Python312) prestane platit pri kazde aktualizaci Pythonu a launcher pak
rem hlasi "Python nenalezen" na stroji, kde Python bezi.

cd /d "%~dp0"

rem ExifTool na PATH pro tento beh - config.py si ho najde i sam, tohle je
rem jen pro pripad, ze by se volal z prikazove radky.
set "PATH=%LOCALAPPDATA%\Programs\ExifTool;%PATH%"

set "PY="

rem 1) python na PATH (bezna instalace z python.org se tam pridava sama)
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined PY set "PY=%%P"
)

rem 2) launcher py.exe umi vybrat nejnovejsi nainstalovanou verzi
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)

rem 3) obvykla mista instalace, od nejnovejsi verze
if not defined PY (
  for %%V in (314 313 312 311 310) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
      set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    )
    if not defined PY if exist "C:\Python%%V\python.exe" set "PY=C:\Python%%V\python.exe"
  )
)

if not defined PY (
  echo.
  echo Python nenalezen. Nainstaluj Python 3.10 nebo novejsi z python.org
  echo a pri instalaci zaskrtni "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

echo Pouzivam Python: %PY%
"%PY%" run.py
pause
