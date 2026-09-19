@echo off
REM ideal_qr_tool\docker.bat - build/run tool qua Docker (Windows).
REM
REM Container hoa de moi truong chay dong nhat giua cac OS - tranh loi
REM khac biet runtime giua macOS/Windows (VD: 403 do curl_cffi TLS
REM fingerprint build khac nhau giua host OS) vi luon chay trong 1 base
REM image Linux co dinh (python:3.13-slim).
REM
REM Usage:
REM   docker.bat up               (build neu can + start container)
REM   docker.bat up --build       (ep build lai image roi start)
REM   docker.bat down             (stop + remove container)
REM   docker.bat restart          (down roi up lai)
REM   docker.bat logs             (follow log realtime)
REM   docker.bat build            (chi build image, khong start)
REM   docker.bat ps               (trang thai container)
REM   docker.bat shell            (mo bash trong container dang chay)
REM
REM Env override:
REM   set BACKEND_PORT=9000 && docker.bat up

setlocal enabledelayedexpansion
cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker khong co tren PATH. Cai Docker Desktop: https://www.docker.com/products/docker-desktop/
    exit /b 1
)

REM Detect docker compose v2 plugin vs standalone v1.
set "COMPOSE=docker compose"
docker compose version >nul 2>&1
if errorlevel 1 (
    where docker-compose >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Khong tim thay 'docker compose' hay 'docker-compose'. Cap nhat Docker Desktop.
        exit /b 1
    )
    set "COMPOSE=docker-compose"
)

set "CMD=%~1"
if "%CMD%"=="" set "CMD=help"
shift

REM Gom cac arg con lai (tu %2 tro di, da shift 1 lan) thanh REST_ARGS.
set "REST_ARGS="
:collect_args
if "%~1"=="" goto :args_done
set "REST_ARGS=%REST_ARGS% %~1"
shift
goto :collect_args
:args_done

if /i "%CMD%"=="up" (
    echo [docker] Build + start container ^(detached^)...
    %COMPOSE% up -d%REST_ARGS%
    if errorlevel 1 exit /b 1
    if not defined BACKEND_PORT set "BACKEND_PORT=8989"
    echo [docker] Container dang chay -^> http://127.0.0.1:%BACKEND_PORT%/
    echo [docker] Xem log: docker.bat logs
    exit /b 0
)

if /i "%CMD%"=="down" (
    echo [docker] Stop + remove container...
    %COMPOSE% down%REST_ARGS%
    exit /b %ERRORLEVEL%
)

if /i "%CMD%"=="restart" (
    echo [docker] Restart container...
    %COMPOSE% down
    %COMPOSE% up -d%REST_ARGS%
    if not defined BACKEND_PORT set "BACKEND_PORT=8989"
    echo [docker] Container dang chay -^> http://127.0.0.1:%BACKEND_PORT%/
    exit /b 0
)

if /i "%CMD%"=="build" (
    echo [docker] Build image ^(khong start^)...
    %COMPOSE% build%REST_ARGS%
    exit /b %ERRORLEVEL%
)

if /i "%CMD%"=="logs" (
    %COMPOSE% logs -f --tail=200%REST_ARGS%
    exit /b %ERRORLEVEL%
)

if /i "%CMD%"=="ps" (
    %COMPOSE% ps%REST_ARGS%
    exit /b %ERRORLEVEL%
)

if /i "%CMD%"=="shell" (
    echo [docker] Mo bash trong container ideal-qr-tool...
    docker exec -it ideal-qr-tool bash
    exit /b %ERRORLEVEL%
)

echo Usage: docker.bat ^<command^> [args]
echo.
echo Commands:
echo   up [--build]     build (neu can) + start container (detached)
echo   down             stop + remove container
echo   restart          down roi up lai
echo   build            chi build image, khong start
echo   logs             follow log realtime (Ctrl+C de thoat)
echo   ps               trang thai container
echo   shell            mo bash trong container dang chay
echo.
echo Env override:
echo   set BACKEND_PORT=9000 ^&^& docker.bat up
exit /b 0
