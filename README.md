# iDEAL QR Tool

iDEAL QR Tool is a local-operator application that creates iDEAL payment links and QR PNG artifacts for ChatGPT Plus jobs. A FastAPI backend owns job execution and persistence; a Vue 3 single-page UI provides the operations console.

This README is the concise operator and developer entry point. The implementation was checked on 2026-07-12. See the [documentation index](#documentation) for deeper references.

## Requirements

- Python `>= 3.11`
- Node.js `>= 18` for frontend installation and builds
- macOS, Linux, or Windows; the repository contains platform-specific setup helpers
- A configured iDEAL-compatible account line and, when used, working proxy settings

## Quick Start

The setup helper can install dependencies, build the frontend, seed minimum settings, and start Uvicorn:

```bash
bash setup.sh
```

Useful setup options:

```bash
bash setup.sh --skip-run
bash setup.sh --host 127.0.0.1 --port 8989
bash setup.sh --db /absolute/path/ideal_qr_tool.db
bash setup.sh --rebuild-frontend
```

For a manual installation:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd ../frontend
npm install
npm run build
cd ..
./dev.sh
```

Open `http://127.0.0.1:8989/`. `dev.sh` builds `frontend/dist/` when needed and runs Uvicorn with reload. The default SQLite database is `backend/runtime/ideal_qr_tool.db`.

## Daily Use

The UI accepts one account line per row, submits jobs, streams status and log events, and exposes settings, proxy probing, and Telegram controls. Job states include `pending`, `running`, `qr_ready`, `error`, and `stopped`; a pending job can also be held until explicitly started.

The UI supports:

- submit, add-held, start, stop, rerun, remove, QR download, payment-link copy, and plan checks
- bulk start-held, stop, retry-failed, clear-failed, and clear-all actions
- payment methods: iDEAL, UPI (vendor), UPI no CDK (OaiPay), **UPI direct** (in-house ChatGPT+Stripe)
- iDEAL / UPI / UPI direct / proxy / session-cache / Telegram settings
- dark mode by default with an in-memory light-mode toggle

The API is **trusted-local-only**. Redaction does not replace authentication; do not expose the service on untrusted networks without an external auth boundary. See [docs/upi-direct-security.md](docs/upi-direct-security.md) for UPI Direct egress and proxy roles.

Account lines and runtime settings persist in the backend. The UI draft is stored as the SQLite setting `ui.input_draft`; theme state is not persisted.

## CLI

The installed backend exposes `ideal-qr` with `text` or `json` output:

```bash
cd backend
.venv/bin/ideal-qr --format text run 'email|password|totp_secret'
.venv/bin/ideal-qr --format json --db-path runtime/ideal_qr_tool.db run-batch runtime/accounts.txt
.venv/bin/ideal-qr settings get --prefix ideal
.venv/bin/ideal-qr session-cache clear --all
```

`run-batch` reads UTF-8 files, skips blank lines and `#` comments, streams job events, and prints a final summary. Do not run the web runner and CLI against the same SQLite database concurrently unless the operational consequences are understood.

## Development Commands

```bash
npm --prefix frontend test
cd backend && .venv/bin/python -m pytest -q
npm test
cd frontend && npm run build
```

The frontend build runs `vue-tsc --noEmit` followed by `vite build`. The root test runs frontend Vitest and backend pytest. There is no frontend lint or end-to-end script in `frontend/package.json`.

For Vue hot reload, run `./dev.sh` in one terminal and `cd frontend && npm run dev` in another. Vite proxies `/api` to port `8989`, or to the port in `VITE_BACKEND_PORT`.

## Configuration

Bootstrap values are read from environment variables; other runtime settings are validated and stored in SQLite:

```text
BACKEND_HOST
BACKEND_PORT
IDEAL_QR_TOOL_DB_PATH
IDEAL_QR_TOOL_BIND_HOST
VITE_BACKEND_PORT
PYTHON
PYTHONUNBUFFERED
```

`BACKEND_HOST` and `BACKEND_PORT` are launcher values. `IDEAL_QR_TOOL_BIND_HOST` is passed to the application for runtime metadata; Uvicorn's `--host` remains the actual socket bind setting. `PYTHON` is used by `setup.sh`; `PYTHONUNBUFFERED` is set by the Docker image. There is no `.env.example` file.

## Runtime Data and Security

- SQLite is the source of truth for settings and jobs.
- `backend/runtime/session_cache/` contains JSON session bundles; `backend/runtime/qr/` contains QR PNG artifacts.
- Runtime data is ignored by the repository rules, but operators must still protect the directory and backups.
- Logs and SSE payloads use redaction helpers, and proxy output is masked in the proxy API.
- The application has no API authentication. Do not bind it to an untrusted network.
- `account_line` is currently stored in job records and returned by job APIs. It may contain passwords, TOTP secrets, or tokens.
- Session cache files may contain cookies or access tokens and are not documented as encrypted or permission-hardened by the code.

Treat any non-loopback deployment as an unverified security-sensitive configuration requiring an external access-control layer and protected storage.

## Documentation

- [Project overview and PDR](docs/project-overview-pdr.md)
- [Codebase summary](docs/codebase-summary.md)
- [Code standards](docs/code-standards.md)
- [System architecture](docs/system-architecture.md)
- [Deployment guide](docs/deployment-guide.md)
- [Frontend design guidelines](docs/design-guidelines.md)
- [Project roadmap](docs/project-roadmap.md)
- [Historical Telegram pull-mode review](docs/telegram-pull-job-mode_review.md) - internal evidence; its test results are not freshly verified by this documentation pass.

## Known Repository Caveats

- `build.sh` and `build.bat` currently expect `scripts/release/setup.sh` and `scripts/release/setup.bat`, which are absent in this checkout. Treat the release-bundle path as blocked until those templates are restored.
- Root metadata declares a pnpm package manager, while the checked-in scripts and setup flow use npm. Follow the scripts (`npm`) unless the package-manager policy is intentionally changed.
- `backend/test/setup_e2e_settings.py` uses a separate test database convention. Normal application startup uses `backend/runtime/ideal_qr_tool.db` unless overridden.
