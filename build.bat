@echo off
REM ideal_qr_tool - build release bundle (Windows).
REM
REM Output:
REM   release\<name>\           (staging - co the chay truc tiep)
REM   release\<name>.zip        (Windows-friendly)
REM
REM Xem docstring `build.sh` de biet chi tiet noi dung bundle + exclusion.
REM
REM Usage:
REM   build.bat
REM   build.bat --name my_qr_tool_v1
REM   build.bat --no-archive
REM   build.bat --clean
REM   build.bat --no-timestamp
REM
REM Ten bundle mac dinh: <base>_YYYYMMDD_HHMMSS (append timestamp de phan biet
REM giua cac lan build). Dung --no-timestamp de tat.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "ROOT_DIR=%CD%"
set "BACKEND_DIR=%ROOT_DIR%\backend"
set "FRONTEND_DIR=%ROOT_DIR%\frontend"
set "RELEASE_TEMPLATE_DIR=%ROOT_DIR%\scripts\release"
set "RELEASE_DIR=%ROOT_DIR%\release"

set "BUNDLE_BASE=ideal_qr_tool"
set "NO_ARCHIVE=0"
set "CLEAN=0"
set "NO_TIMESTAMP=0"

:parse_args
if "%~1"=="" goto :parse_done
set "ARG=%~1"
set "VAL=%~2"

if /i "%ARG%"=="-h"      goto :show_help
if /i "%ARG%"=="--help"  goto :show_help

if /i "%ARG%"=="--name" (
    if "%VAL%"=="" goto :arg_missing
    set "BUNDLE_BASE=%VAL%"
    shift & shift
    goto :parse_args
)
if /i "%ARG%"=="--no-archive" (
    set "NO_ARCHIVE=1"
    shift
    goto :parse_args
)
if /i "%ARG%"=="--clean" (
    set "CLEAN=1"
    shift
    goto :parse_args
)
if /i "%ARG%"=="--no-timestamp" (
    set "NO_TIMESTAMP=1"
    shift
    goto :parse_args
)

echo ERROR: unknown arg: %ARG%
exit /b 1

:arg_missing
echo ERROR: %ARG% thieu value
exit /b 1

:show_help
echo Usage: build.bat [OPTIONS]
echo.
echo Options:
echo   --name NAME       base name cua bundle (default: ideal_qr_tool)
echo                     Timestamp _YYYYMMDD_HHMMSS se duoc append tru khi co --no-timestamp.
echo   --no-archive      chi tao thu muc staging, khong dong goi
echo   --clean           xoa release\ truoc khi build
echo   --no-timestamp    KHONG append timestamp vao ten bundle
echo   -h, --help        in help
echo.
echo Vi du:
echo   build.bat                             (ideal_qr_tool_20260706_143025)
echo   build.bat --name qr_v1                (qr_v1_20260706_143025)
echo   build.bat --name qr_v1 --no-timestamp (qr_v1)
exit /b 0

:parse_done

REM Sinh BUILD_TS = YYYYMMDD_HHMMSS (khong phu thuoc locale, dung PowerShell)
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "BUILD_TS=%%i"
if not defined BUILD_TS (
    echo ERROR: Khong sinh duoc BUILD_TS ^(PowerShell khong kha dung?^)
    exit /b 1
)

if "%NO_TIMESTAMP%"=="1" (
    set "BUNDLE_NAME=%BUNDLE_BASE%"
) else (
    set "BUNDLE_NAME=%BUNDLE_BASE%_%BUILD_TS%"
)

set "STAGING_DIR=%RELEASE_DIR%\%BUNDLE_NAME%"

REM ----- Sanity check -----
if not exist "%RELEASE_TEMPLATE_DIR%\setup.sh" (
    echo ERROR: Missing template %RELEASE_TEMPLATE_DIR%\setup.sh
    exit /b 1
)
if not exist "%RELEASE_TEMPLATE_DIR%\setup.bat" (
    echo ERROR: Missing template %RELEASE_TEMPLATE_DIR%\setup.bat
    exit /b 1
)
if not exist "%BACKEND_DIR%\pyproject.toml" (
    echo ERROR: Missing backend\pyproject.toml
    exit /b 1
)
if not exist "%BACKEND_DIR%\test\setup_default_settings.py" (
    echo ERROR: Missing backend\test\setup_default_settings.py
    exit /b 1
)

echo.
echo ==============================================================
echo   ideal_qr_tool - build release (Windows)
echo   bundle:   %BUNDLE_NAME%
echo   build ts: %BUILD_TS%
echo   staging:  %STAGING_DIR%
echo ==============================================================
echo.

REM ----- [1/6] Clean staging -----
if "%CLEAN%"=="1" (
    echo [1/6] Clean %RELEASE_DIR%
    if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
) else if exist "%STAGING_DIR%" (
    echo [1/6] Xoa staging cu: %STAGING_DIR%
    rmdir /s /q "%STAGING_DIR%"
) else (
    echo [1/6] Staging chua ton tai - skip clean
)
mkdir "%STAGING_DIR%"

REM ----- [2/6] Build frontend -----
echo [2/6] Build frontend production ^(vite build^)...
where node >nul 2>&1
if errorlevel 1 (
    echo ERROR: Node.js khong tren PATH. Chay 'setup.bat --skip-run' truoc.
    exit /b 1
)
where npm >nul 2>&1
if errorlevel 1 (
    echo ERROR: npm khong tren PATH.
    exit /b 1
)

cd /d "%FRONTEND_DIR%"
if not exist "node_modules" (
    echo   npm install ^(chua co node_modules^)...
    call npm install --no-audit --no-fund || goto :fail_npm
)

if exist "dist" rmdir /s /q "dist"

set "NODE_ENV=production"
call npx --yes vite build || goto :fail_build

if not exist "dist\index.html" (
    echo ERROR: Build frontend fail: dist\index.html khong ton tai
    exit /b 1
)

REM Xoa sourcemap neu co (defensive)
for /r "dist" %%f in (*.map) do del /f /q "%%f" >nul 2>&1

echo   frontend\dist built OK

REM ----- [3/6] Copy backend -----
echo [3/6] Copy backend\ ^-^> staging ^(strip cache/test^)...
cd /d "%ROOT_DIR%"

set "STAGING_BACKEND=%STAGING_DIR%\backend"

REM robocopy exit code 0-7 = success, 8+ = fail.
robocopy "%BACKEND_DIR%" "%STAGING_BACKEND%" /E ^
  /XD .venv __pycache__ .pytest_cache .hypothesis .mypy_cache .ruff_cache test tests ^
  /XF *.pyc ^
  /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if errorlevel 8 goto :fail_copy_backend

REM Runtime: xoa noi dung, chi giu .gitkeep
if exist "%STAGING_BACKEND%\runtime" (
    for /f "delims=" %%f in ('dir /b /a-d "%STAGING_BACKEND%\runtime\*" 2^>nul') do (
        if /i not "%%f"==".gitkeep" del /f /q "%STAGING_BACKEND%\runtime\%%f"
    )
    for /f "delims=" %%d in ('dir /b /ad "%STAGING_BACKEND%\runtime\*" 2^>nul') do (
        rmdir /s /q "%STAGING_BACKEND%\runtime\%%d"
    )
)
if not exist "%STAGING_BACKEND%\runtime" mkdir "%STAGING_BACKEND%\runtime"
if not exist "%STAGING_BACKEND%\runtime\.gitkeep" type nul > "%STAGING_BACKEND%\runtime\.gitkeep"

REM Copy seed script -> backend\seed_settings.py
copy /y "%BACKEND_DIR%\test\setup_default_settings.py" "%STAGING_BACKEND%\seed_settings.py" >nul

REM ----- [4/6] Copy frontend\dist -----
echo [4/6] Copy frontend\dist ^-^> staging...
set "STAGING_FRONTEND=%STAGING_DIR%\frontend"
mkdir "%STAGING_FRONTEND%"
robocopy "%FRONTEND_DIR%\dist" "%STAGING_FRONTEND%\dist" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if errorlevel 8 goto :fail_copy_frontend

REM Defensive: check khong co .vue file
dir /s /b "%STAGING_DIR%\*.vue" >nul 2>&1
if not errorlevel 1 (
    echo ERROR: Bundle van con .vue file - logic copy sai!
    exit /b 1
)

REM ----- [5/6] Copy setup templates -----
echo [5/6] Copy setup templates ^(release mode^)...
copy /y "%RELEASE_TEMPLATE_DIR%\setup.sh" "%STAGING_DIR%\setup.sh" >nul
copy /y "%RELEASE_TEMPLATE_DIR%\setup.bat" "%STAGING_DIR%\setup.bat" >nul

REM ----- [6/6] Archive -----
if "%NO_ARCHIVE%"=="1" (
    echo [6/6] --no-archive -^> skip dong goi
    goto :summary
)

echo [6/6] Dong goi archive...
cd /d "%RELEASE_DIR%"
set "ZIPBALL=%BUNDLE_NAME%.zip"
if exist "%ZIPBALL%" del /f /q "%ZIPBALL%"

REM Windows 10+ co powershell Compress-Archive.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%BUNDLE_NAME%' -DestinationPath '%ZIPBALL%' -Force" || goto :fail_zip
echo   OK %ZIPBALL%

REM tar.gz (Windows 10+ co tar.exe built-in)
where tar >nul 2>&1
if not errorlevel 1 (
    tar -czf "%BUNDLE_NAME%.tar.gz" "%BUNDLE_NAME%"
    echo   OK %BUNDLE_NAME%.tar.gz
)

:summary
echo.
echo ==============================================================
echo   Build release done
echo   Staging: %STAGING_DIR%
if "%NO_ARCHIVE%"=="0" (
    echo   Archive: %RELEASE_DIR%\%BUNDLE_NAME%.zip
)
echo ==============================================================
echo.
echo Test bundle:
echo   cd %STAGING_DIR% ^&^& setup.bat
exit /b 0


:fail_npm
echo ERROR: npm install that bai.
exit /b 1

:fail_build
echo ERROR: vite build that bai.
exit /b 1

:fail_copy_backend
echo ERROR: robocopy backend that bai.
exit /b 1

:fail_copy_frontend
echo ERROR: robocopy frontend\dist that bai.
exit /b 1

:fail_zip
echo ERROR: Compress-Archive that bai.
exit /b 1
