@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\runtime\python\python.exe"
rem -s keeps the bundled runtime from loading packages from the user's own Python.
set "PYTHON_FLAGS=-s"
if not exist "%PYTHON%" set "PYTHON_FLAGS="
if not exist "%PYTHON%" if defined KYF_PYTHON set "PYTHON=%KYF_PYTHON%"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" %PYTHON_FLAGS% "%ROOT%\scripts\start-real-client.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" if "%~1"=="" pause
exit /b %RESULT%
