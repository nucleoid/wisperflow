@echo off
setlocal
pushd "%~dp0"
".venv\Scripts\python.exe" main.py %*
set RC=%ERRORLEVEL%
popd
exit /b %RC%
