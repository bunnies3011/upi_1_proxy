@echo off
REM ideal_qr_tool - fast start (Windows, khong cai lai dependencies)
REM
REM Usage:
REM   start.bat
REM   start.bat --port 8085
REM   start.bat --host 0.0.0.0 --port 9000
REM   start.bat --db D:\data\ideal_qr_tool.db
REM   start.bat --rebuild-frontend
REM   start.bat --help

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "ROOT_DIR=%CD%"
set "BACKEND_DIR=%ROOT_DIR%\backend"
set "FRONTEND_DIR=%ROOT_DIR%\frontend"
set "FRONTEND_DIST=%FRONTEND_DIR%\dist"

if not defined BACKEND_HOST set "BACKEND_HOST=127.0.0.1"
if not defined BACKEND_PORT set "BACKEND_PORT=8989"
if not defined IDEAL_QR_TOOL_DB_PATH set "IDEAL_QR_TOOL_DB_PATH="

set "REBUILD_FE=0"

:parse_args
if "%~1"=="" goto :parse_done
set "ARG=%~1"
set "VAL=%~2"

if /i "%ARG%"=="-h" goto :show_help
if /i "%ARG%"=="--help" goto :show_help

if /i "%ARG%"=="--port" (
    if "%VAL%"=="" goto :arg_missing
    set "BACKEND_PORT=%VAL%"
    shift & shift
    goto :parse_args
)
if /i "%ARG%"=="--host" (
    if "%VAL%"=="" goto :arg_missing
    set "BACKEND_HOST=%VAL%"
    shift & shift
    goto :parse_args
)
if /i "%ARG%"=="--db" (
    if "%VAL%"=="" goto :arg_missing
    set "IDEAL_QR_TOOL_DB_PATH=%VAL%"
    shift & shift
    goto :parse_args
)
if /i "%ARG%"=="--rebuild-frontend" (
    set "REBUILD_FE=1"
    shift
    goto :parse_args
)

echo ERROR: unknown arg: %ARG%
echo Xem: start.bat --help
exit /b 1

:arg_missing
echo ERROR: %ARG% thieu value
exit /b 1

:show_help
echo Usage: start.bat [OPTIONS]
echo.
echo Options:
echo   --host H              bind host (default 127.0.0.1)
echo   --port N              bind port (default 8989)
echo   --db PATH             override SQLite DB path
echo   --rebuild-frontend    ep build lai frontend\dist bang vite
echo   -h, --help            in help
echo.
echo Script nay KHONG cai lai dependency.
echo Neu chua setup lan dau, chay: setup.bat
exit /b 0

:parse_done

if not exist "%BACKEND_DIR%\.venv\Scripts\uvicorn.exe" (
    echo ERROR: chua co backend\.venv\Scripts\uvicorn.exe
    echo Chay setup lan dau: .\setup.bat --skip-run
    exit /b 1
)

if not exist "%BACKEND_DIR%\.venv\Scripts\python.exe" (
    echo ERROR: chua co backend\.venv\Scripts\python.exe
    echo Chay setup lan dau: .\setup.bat --skip-run
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\nodejs\node.exe" set "PATH=%ProgramFiles%\nodejs;%PATH%"
    if exist "%ProgramFiles(x86)%\nodejs\node.exe" set "PATH=%ProgramFiles(x86)%\nodejs;%PATH%"
    if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" set "PATH=%LOCALAPPDATA%\Programs\nodejs;%PATH%"
)

if "%REBUILD_FE%"=="1" (
    if not exist "%FRONTEND_DIR%\node_modules" (
        echo ERROR: frontend\node_modules chua co.
        echo Chay setup lan dau: .\setup.bat --skip-run
        exit /b 1
    )
    echo [start] Build frontend ...
    cd /d "%FRONTEND_DIR%"
    call npx vite build
    if errorlevel 1 (
        echo ERROR: vite build that bai.
        exit /b 1
    )
    cd /d "%ROOT_DIR%"
)

if not exist "%FRONTEND_DIST%\index.html" (
    echo ERROR: chua co frontend\dist\index.html
    echo Chay 1 trong 2 lenh:
    echo   .\setup.bat --skip-run
    echo   .\start.bat --rebuild-frontend
    exit /b 1
)

echo.
echo ==============================================================
echo   ideal_qr_tool - fast start
echo   root: %ROOT_DIR%
echo   URL : http://%BACKEND_HOST%:%BACKEND_PORT%/
if defined IDEAL_QR_TOOL_DB_PATH if not "%IDEAL_QR_TOOL_DB_PATH%"=="" echo   DB  : %IDEAL_QR_TOOL_DB_PATH%
echo ==============================================================
echo.

cd /d "%BACKEND_DIR%"
set "IDEAL_QR_TOOL_BIND_HOST=%BACKEND_HOST%"
"%BACKEND_DIR%\.venv\Scripts\uvicorn.exe" app.main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --no-use-colors
exit /b %ERRORLEVEL%
