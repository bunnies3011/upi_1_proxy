@echo off
REM ideal_qr_tool - 1 lenh setup full + start (Windows).
REM
REM Khong quan tam may dang co san gi: script se
REM   1. Detect / auto-install Python 3.13 (winget hoac python.org silent).
REM   2. Detect / auto-install Node.js LTS (winget hoac nodejs.org MSI).
REM   3. Tao backend\.venv + pip install -e ".[dev]".
REM   4. npm install trong frontend\.
REM   5. Build frontend (bypass vue-tsc, giong dev.sh).
REM   6. Seed Settings toi thieu (default_issuer + 1 device profile NL).
REM   7. Start uvicorn - serve ca UI Vue + REST API cung origin.
REM
REM Usage:
REM   setup.bat                            (defaults: 127.0.0.1:8989)
REM   setup.bat --port 9000
REM   setup.bat --host 0.0.0.0 --port 8080
REM   setup.bat --rebuild-frontend
REM   setup.bat --skip-run
REM   setup.bat --db D:\data\test.db
REM   setup.bat --help
REM
REM Env override: BACKEND_HOST, BACKEND_PORT, IDEAL_QR_TOOL_DB_PATH, PYTHON

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "ROOT_DIR=%CD%"
set "BACKEND_DIR=%ROOT_DIR%\backend"
set "FRONTEND_DIR=%ROOT_DIR%\frontend"
set "FRONTEND_DIST=%FRONTEND_DIR%\dist"

set "PY_VERSION_PIN=3.13.1"
set "PY_INSTALL_DIR=%LOCALAPPDATA%\Programs\Python\Python313"

REM ----- Track DB source truoc khi apply defaults -----
REM _DB_FROM_ENV=1 neu env IDEAL_QR_TOOL_DB_PATH da co gia tri tu ngoai.
set "_DB_FROM_ENV=0"
if defined IDEAL_QR_TOOL_DB_PATH (
    if not "%IDEAL_QR_TOOL_DB_PATH%"=="" set "_DB_FROM_ENV=1"
)
set "_DB_FROM_CLI=0"

REM ----- Defaults (env override neu co) -----
if not defined BACKEND_HOST set "BACKEND_HOST=127.0.0.1"
if not defined BACKEND_PORT set "BACKEND_PORT=8989"
if not defined IDEAL_QR_TOOL_DB_PATH set "IDEAL_QR_TOOL_DB_PATH="

set "REBUILD_FE=0"
set "SKIP_RUN=0"

REM ----- Parse args -----
:parse_args
if "%~1"=="" goto :parse_done
set "ARG=%~1"
set "VAL=%~2"

if /i "%ARG%"=="-h"      goto :show_help
if /i "%ARG%"=="--help"  goto :show_help

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
    set "_DB_FROM_CLI=1"
    shift & shift
    goto :parse_args
)
if /i "%ARG%"=="--rebuild-frontend" (
    set "REBUILD_FE=1"
    shift
    goto :parse_args
)
if /i "%ARG%"=="--skip-run" (
    set "SKIP_RUN=1"
    shift
    goto :parse_args
)

echo ERROR: unknown arg: %ARG%
echo Xem: setup.bat --help
exit /b 1

:arg_missing
echo ERROR: %ARG% thieu value
exit /b 1

:show_help
echo Usage: setup.bat [OPTIONS]
echo.
echo Options:
echo   --host H              bind host (default 127.0.0.1)
echo   --port N              bind port (default 8989)
echo   --db PATH             SQLite DB path (default: backend\runtime\ideal_qr_tool.db
echo                          khi --port=8989; cac port khac auto dung
echo                          ideal_qr_tool_^<PORT^>.db de moi instance co DB rieng)
echo   --rebuild-frontend    ep build lai frontend\dist
echo   --skip-run            chi setup xong roi thoat, KHONG start uvicorn
echo   -h, --help            in help
echo.
echo Neu may chua co Python 3.13, script se tu dong cai user-scope
echo (khong can admin) qua winget hoac installer python.org.
echo Node.js LTS auto-install qua winget hoac nodejs.org.
echo.
echo Env override: BACKEND_HOST, BACKEND_PORT, IDEAL_QR_TOOL_DB_PATH, PYTHON
exit /b 0

:parse_done

REM ----- Per-port DB isolation -----
REM Neu user khong tu set DB (--db hoac env) VA port khac default 8989 ->
REM auto derive DB path theo port (moi port mot DB rieng).
if "%_DB_FROM_ENV%"=="0" if "%_DB_FROM_CLI%"=="0" if not "%BACKEND_PORT%"=="8989" (
    set "IDEAL_QR_TOOL_DB_PATH=%BACKEND_DIR%\runtime\ideal_qr_tool_%BACKEND_PORT%.db"
)

echo.
echo ==============================================================
echo   ideal_qr_tool - auto setup + start (Windows)
echo   root: %ROOT_DIR%
echo ==============================================================
echo.

REM =====================================================================
REM [1/7] Detect / auto-install Python 3.11+
REM =====================================================================
echo [1/7] Detect Python ^(^>= 3.11^)...

set "PY_BIN="

if defined PYTHON (
    set "PY_BIN=%PYTHON%"
    goto :verify_python
)

REM Preferred: auto-installed path (Python 3.13 user-scope).
if exist "%PY_INSTALL_DIR%\python.exe" (
    set "PY_BIN=%PY_INSTALL_DIR%\python.exe"
    goto :verify_python
)

REM Fallback: cac Python user-scope khac (3.12 / 3.11) neu 3.13 chua co.
for %%d in (Python312 Python311) do (
    if exist "%LOCALAPPDATA%\Programs\Python\%%d\python.exe" (
        set "PY_BIN=%LOCALAPPDATA%\Programs\Python\%%d\python.exe"
        goto :verify_python
    )
)

REM System-wide install (all-users).
for %%d in (Python313 Python312 Python311) do (
    if exist "%ProgramFiles%\%%d\python.exe" (
        set "PY_BIN=%ProgramFiles%\%%d\python.exe"
        goto :verify_python
    )
)

REM py -3.13 -> py -3.12 -> py -3.11
for %%v in (3.13 3.12 3.11) do (
    py -%%v --version >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%p in ('py -%%v -c "import sys; print(sys.executable)"') do set "PY_BIN=%%p"
        goto :verify_python
    )
)

REM python3.13 / python3.12 / python3.11 on PATH
for %%c in (python3.13 python3.12 python3.11) do (
    where %%c >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%p in ('%%c -c "import sys; print(sys.executable)"') do set "PY_BIN=%%p"
        goto :verify_python
    )
)

REM python -> check version
where python >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)"') do set "PY_BIN=%%p"
    "!PY_BIN!" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 goto :verify_python
    set "PY_BIN="
)

REM Khong tim thay -> auto-install
call :auto_install_python
if errorlevel 1 exit /b 1
set "PY_BIN=%PY_INSTALL_DIR%\python.exe"

:verify_python
if not exist "%PY_BIN%" (
    echo ERROR: PY_BIN "%PY_BIN%" khong ton tai.
    exit /b 1
)
"%PY_BIN%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: "%PY_BIN%" ^< Python 3.11. Set PYTHON toi interpreter ^>= 3.11 hoac unset de auto-detect.
    exit /b 1
)
REM Doc Python version qua temp file - cmd's `for /f` co bug voi nested
REM quotes khi executable path duoc quote va co them arg quote (`"path" -c "..."`).
REM Cmd /c strip outer pair sai va break parse. Redirect stdout ra file
REM roi `set /p` doc lai - 100%% reliable, khong phu thuoc quoting quirk.
set "PY_VERSION="
"%PY_BIN%" -c "import sys; print(sys.version.split()[0])" > "%TEMP%\_iqrt_pyver.tmp" 2>nul
if exist "%TEMP%\_iqrt_pyver.tmp" (
    set /p PY_VERSION=<"%TEMP%\_iqrt_pyver.tmp"
    del "%TEMP%\_iqrt_pyver.tmp" >nul 2>&1
)
echo   python: %PY_BIN% ^(%PY_VERSION%^)

REM =====================================================================
REM [2/7] Detect / auto-install Node.js 18+
REM =====================================================================
echo [2/7] Detect Node.js ^(^>= 18^)...

REM Truoc khi hoi `where node`, tim node tai cac vi tri quen thuoc va
REM prepend vao PATH. Truong hop pho bien: user vua chay setup.bat lan
REM truoc (da cai Node MSI vao %ProgramFiles%\nodejs) roi mo cmd moi ->
REM `where node` fail vi PATH cua cmd hien tai chua refresh. Neu bo qua
REM buoc nay, script se download va re-install MSI vo ich.
call :probe_and_prepend_node_path

set "NODE_MAJOR=0"
where node >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%m in ('node -p "process.versions.node.split('.')[0]"') do set "NODE_MAJOR=%%m"
)

if %NODE_MAJOR% LSS 18 (
    echo   Node ^< 18 hoac khong co. Auto-install...
    call :auto_install_node
    if errorlevel 1 exit /b 1
    REM Sau khi auto-install, probe lai lan nua truoc khi doc version.
    call :probe_and_prepend_node_path
    for /f "delims=" %%m in ('node -p "process.versions.node.split('.')[0]"') do set "NODE_MAJOR=%%m"
    if !NODE_MAJOR! LSS 18 (
        echo ERROR: auto-install Node xong nhung version van ^< 18.
        exit /b 1
    )
)

for /f "delims=" %%n in ('node -v') do set "NODE_VERSION=%%n"
for /f "delims=" %%n in ('npm -v') do set "NPM_VERSION=%%n"
echo   node: %NODE_VERSION%, npm: %NPM_VERSION%

REM =====================================================================
REM [3/7] Backend venv
REM =====================================================================
echo [3/7] Backend virtualenv...
cd /d "%BACKEND_DIR%"

set "RECREATE_VENV=0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo   .venv ^< Python 3.11 - recreate.
        set "RECREATE_VENV=1"
    ) else (
        REM Doc venv version qua temp file (cung li do voi PY_VERSION o tren).
        set "VENV_VER="
        ".venv\Scripts\python.exe" -c "import sys; print(sys.version.split()[0])" > "%TEMP%\_iqrt_venvver.tmp" 2>nul
        if exist "%TEMP%\_iqrt_venvver.tmp" (
            set /p VENV_VER=<"%TEMP%\_iqrt_venvver.tmp"
            del "%TEMP%\_iqrt_venvver.tmp" >nul 2>&1
        )
        echo   .venv exists ^(python !VENV_VER!^) OK
    )
) else (
    set "RECREATE_VENV=1"
)

if "%RECREATE_VENV%"=="1" (
    echo   creating .venv...
    if exist .venv rmdir /s /q .venv
    "%PY_BIN%" -m venv .venv || goto :fail_venv
)

set "VENV_PY=%BACKEND_DIR%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo ERROR: %VENV_PY% khong ton tai sau khi tao venv.
    exit /b 1
)

REM =====================================================================
REM [4/7] pip install backend deps
REM =====================================================================
echo [4/7] Backend deps...
set "NEED_PIP_INSTALL=0"
if not exist ".venv\.iqrt_pyproject.stamp" (
    set "NEED_PIP_INSTALL=1"
) else if exist "pyproject.toml" (
    for /f "delims=" %%r in ('powershell -NoProfile -Command "if ((Get-Item pyproject.toml).LastWriteTime.Ticks -gt (Get-Item .venv\.iqrt_pyproject.stamp).LastWriteTime.Ticks) { 'YES' } else { 'NO' }"') do set "PYPROJECT_NEWER=%%r"
    if /i "!PYPROJECT_NEWER!"=="YES" set "NEED_PIP_INSTALL=1"
)

if "%NEED_PIP_INSTALL%"=="1" (
    echo   pip install -e ".[dev]" ...
    "%VENV_PY%" -m pip install -q --upgrade pip || goto :fail_pip
    "%VENV_PY%" -m pip install -q -e ".[dev]" || goto :fail_pip
    copy /y "pyproject.toml" ".venv\.iqrt_pyproject.stamp" >nul || goto :fail_pip
    echo   backend deps installed OK
) else (
    echo   backend deps da co OK
)

REM =====================================================================
REM [5/7] Frontend npm install
REM =====================================================================
echo [5/7] Frontend npm install...
cd /d "%FRONTEND_DIR%"

if not exist "node_modules" (
    call npm install --no-audit --no-fund
    if errorlevel 1 goto :fail_npm
) else (
    REM Neu package-lock.json moi hon marker `.package-lock.json` npm tao
    REM sau khi install xong -> chac chan dependency da doi -> reinstall.
    REM Dung PowerShell so sanh mtime dang so nguyen (100ns ticks) de
    REM tranh locale-dependent string compare cua `%%~tf` (VD MM/DD/YYYY
    REM sort loi qua bien nam).
    set "NEED_NPM_INSTALL=0"
    if not exist "node_modules\.package-lock.json" (
        set "NEED_NPM_INSTALL=1"
    ) else if exist "package-lock.json" (
        for /f "delims=" %%r in ('powershell -NoProfile -Command "if ((Get-Item package-lock.json).LastWriteTime.Ticks -gt (Get-Item node_modules\.package-lock.json).LastWriteTime.Ticks) { 'YES' } else { 'NO' }"') do set "LOCK_NEWER=%%r"
        if /i "!LOCK_NEWER!"=="YES" set "NEED_NPM_INSTALL=1"
    )
    if "!NEED_NPM_INSTALL!"=="1" (
        echo   package-lock.json moi hon - npm install lai
        call npm install --no-audit --no-fund
        if errorlevel 1 goto :fail_npm
    ) else (
        echo   node_modules da co OK
    )
)

REM =====================================================================
REM [6/7] Build frontend (bypass vue-tsc)
REM =====================================================================
echo [6/7] Build frontend ^(vite^)...
set "NEED_BUILD=0"
if "%REBUILD_FE%"=="1" set "NEED_BUILD=1"
if not exist "%FRONTEND_DIST%\index.html" set "NEED_BUILD=1"

if "%NEED_BUILD%"=="1" (
    call npx --yes vite build
    if errorlevel 1 goto :fail_build
    echo   frontend built -^> %FRONTEND_DIST%
) else (
    echo   frontend\dist da co OK ^(--rebuild-frontend de build lai^)
)

REM =====================================================================
REM [7/7] Seed Settings
REM =====================================================================
echo [7/7] Seed Settings toi thieu...
cd /d "%BACKEND_DIR%"

if not exist "runtime" mkdir "runtime"
if not exist "runtime\session_cache" mkdir "runtime\session_cache"
if not exist "runtime\qr" mkdir "runtime\qr"

REM IDEAL_QR_TOOL_DB_PATH da duoc set (rong hoac co gia tri) o dau script,
REM nen da inherit sang subprocess Python duoi day. Seed script tu handle
REM "empty -> default path".
"%VENV_PY%" test\setup_default_settings.py
if errorlevel 1 goto :fail_seed

REM =====================================================================
REM Summary + start uvicorn
REM =====================================================================
echo.
echo ==============================================================
echo   Setup done
echo   -^> http://%BACKEND_HOST%:%BACKEND_PORT%/
if defined IDEAL_QR_TOOL_DB_PATH if not "%IDEAL_QR_TOOL_DB_PATH%"=="" (
    echo   DB: %IDEAL_QR_TOOL_DB_PATH%
) else (
    echo   DB: backend\runtime\ideal_qr_tool.db
)
echo ==============================================================
echo.

if "%SKIP_RUN%"=="1" (
    echo --skip-run -^> khong start uvicorn.
    echo Chay tay: cd backend ^&^& .venv\Scripts\uvicorn app.main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --no-use-colors
    exit /b 0
)

set "IDEAL_QR_TOOL_BIND_HOST=%BACKEND_HOST%"
REM Disable uvicorn ANSI colors: cmd.exe cu (Win Server, WinPE, phien ban
REM Win10 chua bat Virtual Terminal Processing) khong parse escape sequence
REM -> log hien thi rac dang [32mINFO[0m. Windows Terminal / PowerShell 7
REM van doc log ro rang khong can mau.
"%BACKEND_DIR%\.venv\Scripts\uvicorn.exe" app.main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --no-use-colors
set "WEB_RC=%ERRORLEVEL%"

if not "%WEB_RC%"=="0" (
    echo.
    echo Uvicorn thoat voi ma loi %WEB_RC%.
    pause
)
exit /b %WEB_RC%


REM =====================================================================
REM Subroutines
REM =====================================================================

:probe_and_prepend_node_path
REM Neu `node` chua tren PATH, do 3 vi tri quen thuoc va prepend PATH.
REM Idempotent: goi bao nhieu lan cung khong hai (path duplicate trong
REM PATH la binh thuong, Windows deduplicate khi resolve).
where node >nul 2>&1
if not errorlevel 1 exit /b 0

if exist "%ProgramFiles%\nodejs\node.exe" (
    set "PATH=%ProgramFiles%\nodejs;%PATH%"
    exit /b 0
)
if exist "%ProgramFiles(x86)%\nodejs\node.exe" (
    set "PATH=%ProgramFiles(x86)%\nodejs;%PATH%"
    exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\nodejs;%PATH%"
    exit /b 0
)
exit /b 0


:auto_install_python
echo.
echo Python 3.11+ khong tim thay tren PATH. Auto-installing user-scope...
echo   Target: %PY_INSTALL_DIR%
echo.

REM Method 1: winget
where winget >nul 2>&1
if not errorlevel 1 (
    echo   [install] winget install Python.Python.3.13 --scope user ...
    winget install --id Python.Python.3.13 -e --scope user --silent --accept-source-agreements --accept-package-agreements
    if exist "%PY_INSTALL_DIR%\python.exe" (
        echo   [install] winget OK.
        exit /b 0
    )
    echo   [install] winget khong tao "%PY_INSTALL_DIR%\python.exe", thu direct download...
)

REM Method 2: direct download tu python.org
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION_PIN%/python-%PY_VERSION_PIN%-amd64.exe"
set "PY_INSTALLER=%TEMP%\iqrt_py%PY_VERSION_PIN%_installer.exe"

echo   [install] Downloading %PY_URL%
if exist "%PY_INSTALLER%" del "%PY_INSTALLER%" >nul 2>&1

curl.exe --version >nul 2>&1
if not errorlevel 1 (
    curl.exe -fSL --retry 3 --retry-delay 2 -o "%PY_INSTALLER%" "%PY_URL%"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%' -UseBasicParsing } catch { exit 1 }"
)

if not exist "%PY_INSTALLER%" (
    echo ERROR: khong download duoc Python installer.
    echo   Tai thu cong tu https://www.python.org/downloads/ roi chay lai setup.bat.
    exit /b 1
)

echo   [install] Silent install -^> %PY_INSTALL_DIR% ...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0 SimpleInstall=1 TargetDir="%PY_INSTALL_DIR%"
set "INST_RC=%ERRORLEVEL%"
del "%PY_INSTALLER%" >nul 2>&1

if not "%INST_RC%"=="0" (
    echo ERROR: silent install exit code %INST_RC%.
    exit /b 1
)

if not exist "%PY_INSTALL_DIR%\python.exe" (
    echo ERROR: install xong nhung "%PY_INSTALL_DIR%\python.exe" khong ton tai.
    exit /b 1
)
echo   [install] Python %PY_VERSION_PIN% installed.
exit /b 0


:auto_install_node
echo.
echo Node.js 18+ khong tim thay. Auto-installing...
echo.

REM Method 1: winget
where winget >nul 2>&1
if not errorlevel 1 (
    echo   [install] winget install OpenJS.NodeJS.LTS ...
    winget install --id OpenJS.NodeJS.LTS -e --silent --accept-source-agreements --accept-package-agreements
    REM Winget khong refresh PATH cua process cha; try refreshenv (Chocolatey)
    REM roi probe cac vi tri quen thuoc de prepend PATH manual.
    call refreshenv >nul 2>&1
    call :probe_and_prepend_node_path
    where node >nul 2>&1
    if not errorlevel 1 (
        echo   [install] winget OK.
        exit /b 0
    )
    echo   [install] winget xong nhung node chua tren PATH, thu direct MSI...
)

REM Method 2: direct MSI download tu nodejs.org
set "NODE_MSI_URL=https://nodejs.org/dist/v20.18.1/node-v20.18.1-x64.msi"
set "NODE_MSI=%TEMP%\iqrt_node_lts.msi"

echo   [install] Downloading %NODE_MSI_URL%
if exist "%NODE_MSI%" del "%NODE_MSI%" >nul 2>&1

curl.exe --version >nul 2>&1
if not errorlevel 1 (
    curl.exe -fSL --retry 3 --retry-delay 2 -o "%NODE_MSI%" "%NODE_MSI_URL%"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%NODE_MSI_URL%' -OutFile '%NODE_MSI%' -UseBasicParsing } catch { exit 1 }"
)

if not exist "%NODE_MSI%" (
    echo ERROR: khong download duoc Node MSI.
    echo   Tai thu cong tu https://nodejs.org/en/download/ roi chay lai setup.bat.
    exit /b 1
)

echo   [install] Silent install Node MSI...
msiexec /i "%NODE_MSI%" /qn /norestart
set "MSI_RC=%ERRORLEVEL%"
del "%NODE_MSI%" >nul 2>&1

if not "%MSI_RC%"=="0" (
    echo ERROR: MSI install exit code %MSI_RC%.
    exit /b 1
)

REM Node MSI mac dinh install vao %ProgramFiles%\nodejs - prepend vao PATH
REM cua process (PATH toan cuc da duoc MSI cap nhat nhung shell hien tai
REM chua thay).
call :probe_and_prepend_node_path
where node >nul 2>&1
if errorlevel 1 (
    echo ERROR: install xong nhung "node" khong tren PATH.
    echo   Mo cmd moi va chay lai setup.bat.
    exit /b 1
)
echo   [install] Node.js installed.
exit /b 0


:fail_venv
echo ERROR: khong tao duoc venv voi "%PY_BIN%".
exit /b 1

:fail_pip
echo ERROR: pip install that bai. Kiem tra network + pyproject.toml.
exit /b 1

:fail_npm
echo ERROR: npm install that bai. Kiem tra network + frontend\package.json.
exit /b 1

:fail_build
echo ERROR: vite build that bai. Kiem tra loi TypeScript / import trong frontend\src.
exit /b 1

:fail_seed
echo ERROR: seed Settings that bai. Kiem tra backend\.venv + pyproject.toml.
exit /b 1
