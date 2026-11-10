@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 skillhub.py %*
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python skillhub.py %*
  ) else (
    echo No encuentro Python 3.9 o superior. Instalalo desde python.org y vuelve a abrir este archivo.
  )
)
pause
