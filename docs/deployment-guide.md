# Deployment Guide

This guide covers the launch paths that exist in the repository. The application currently assumes a trusted operator and has no API authentication.

## Local Setup

Prerequisites are Python `>= 3.11` and Node.js `>= 18`. The supported bootstrap helper is:

```bash
bash setup.sh
```

Use `--skip-run` to install, build, and seed without starting Uvicorn. `--host`, `--port`, `--db`, and `--rebuild-frontend` are implemented options. The helper creates the backend virtual environment, installs `backend` with its dev extras, installs frontend npm dependencies, builds the frontend, creates runtime directories, and seeds default settings.

## Development Server

```bash
./dev.sh
```

This builds the frontend if `frontend/dist/index.html` is absent, then starts `app.main:app` with Uvicorn reload on `127.0.0.1:8989` by default. For Vue hot reload, also run:

```bash
cd frontend
npm run dev
```

Vite serves its own port and proxies `/api` to backend port `8989`, or to `VITE_BACKEND_PORT`.

## Production-Like Local Run

```bash
./production.sh
./production.sh --rebuild
```

This uses the same frontend build and starts Uvicorn without reload. `BACKEND_HOST` and `BACKEND_PORT` change the launcher values; the default is loopback on port `8989`.

## Docker

```bash
./docker.sh up
./docker.sh logs
./docker.sh down
```

The image builds the Vue app with Node 20, runs the backend on Python 3.13, exposes container port `8989`, and mounts `./backend/runtime` at `/app/backend/runtime`. The compose healthcheck requests `/`. The volume persists SQLite, session cache, and QR PNG data across container replacement.

Docker binds the application to `0.0.0.0` inside the container. The published host port is therefore a network boundary, not an authentication boundary.

## Environment Contract

The code and launchers reference these variables:

```text
BACKEND_HOST
BACKEND_PORT
IDEAL_QR_TOOL_DB_PATH
IDEAL_QR_TOOL_BIND_HOST
VITE_BACKEND_PORT
PYTHON
PYTHONUNBUFFERED
```

The default application database is `backend/runtime/ideal_qr_tool.db`. An absolute `IDEAL_QR_TOOL_DB_PATH` is used as-is; a relative value is resolved from `backend/` by `main.py`. `setup.sh` also derives a per-port database when a non-default port is selected and no explicit database path is supplied. Use an explicit path when operating multiple instances.

## Operations And Backups

- Back up the SQLite database together with the matching session-cache and QR directories when those artifacts are needed.
- Protect runtime directories and backups as credential-bearing data.
- Stop the service before moving or restoring a database unless the SQLite consistency procedure has been reviewed.
- Do not share one SQLite path between web and CLI runners concurrently without accepting the resulting scheduling and state-race risk.
- Use the session-cache clear API or CLI when cached account sessions must be invalidated.

## Security Requirements

Do not expose this service to an untrusted network in its current state. Authentication is disabled, job APIs return raw `account_line`, and session-cache JSON may contain cookies or access tokens. A hardened deployment must add access control outside the application, restrict network reachability, protect runtime files and backups, and review proxy credentials and logs.

## Release Bundle Caveat

`build.sh` and `build.bat` currently stop during their sanity check because `scripts/release/setup.sh` and `scripts/release/setup.bat` are absent in this checkout. The Docker and local scripts are separate paths and should not be described as proof that the release-bundle path works.
