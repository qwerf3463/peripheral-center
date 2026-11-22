@echo off
title DeviceFloatPanel

set "PYW=D:\python\pythonw.exe"
set "PY=D:\python\python.exe"

if exist "%PYW%" (
    start "" "%PYW%" "%~dp0app.py" %*
    goto :eof
)

if exist "%PY%" (
    "%PY%" "%~dp0app.py" %*
    if errorlevel 1 pause
    goto :eof
)

echo Python interpreter not found, please check D:\python
pause
