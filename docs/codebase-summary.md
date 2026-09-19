# Codebase Summary

Generated from the refreshed `repomix-output.xml` snapshot and verified against the source tree on 2026-07-12. The snapshot packed 1,723 files because this checkout also contains local agent/tooling material; Repomix excluded five files during its security scan. The summary below focuses on application code, scripts, tests, and documentation rather than those external materials.

## Repository Shape

| Area | Role | Representative paths |
| --- | --- | --- |
| `backend/app` | FastAPI service, job orchestration, payment flow, CLI, and Telegram notifier | `backend/app/main.py`, `backend/app/bootstrap.py` |
| `backend/tests` | Unit, property, and integration tests | `backend/tests/unit`, `backend/tests/property`, `backend/tests/integration` |
| `backend/test` | Smoke, syntax, setup, live, and E2E helper scripts | `backend/test/setup_default_settings.py` |
| `frontend/src` | Vue 3 operations SPA | `frontend/src/App.vue` |
| Root scripts | Local, production-like, Docker, and release helpers | `dev.sh`, `production.sh`, `setup.sh`, `docker.sh` |
| `docs` | Evergreen docs plus a preserved historical review | `docs/` |

The application source is medium-sized and test-heavy: the supplied source inventory reports approximately 26.9k lines in `backend/app`, 21.0k in `backend/tests`, 11.3k in `backend/test`, and 11.7k in `frontend/src`, excluding dependencies and tooling.

## Backend Modules

- **`api/`:** REST routes for jobs, settings, session cache, proxy probes, Telegram controls, and the SSE stream. Pydantic schemas define request and response shapes.
- **`core/`:** SQLite access, settings validation, job repository/manager, proxy pool and health, session cache, SSE broadcasting, payment-flow protocol, and redaction.
- **`payments/ideal/`:** iDEAL namespace registration, account parsing, ChatGPT and Stripe clients, transaction client, profile/device selection, issuer selection, flow orchestration, and QR rendering.
- **`notifiers/telegram/`:** Telegram client, push/pull mode coordination, polling, formatting, callbacks, push-success gate, and live QR capacity gate (per-chat rolling window).
- **`cli/`:** The `ideal-qr` entry point and command implementations for single jobs, batches, settings, and session-cache clearing.

`backend/app/main.py` creates the FastAPI app and lifespan. `backend/app/bootstrap.py` creates shared services and registers the iDEAL handler. The core layer does not import a concrete payment method; registration is concentrated at the bootstrap/payment boundary.

## Frontend Modules

The frontend is a Vue 3 + TypeScript SPA with Pinia and no `vue-router`. `App.vue` is the current single operations screen. Components cover input, job list, logs, successful output, bulk actions, settings, proxy configuration, and Telegram configuration. Composables provide job/settings stores, SSE, proxy probing, push-gate state, live-qr-gate state, and a per-tab client ID.

The SSE client uses `fetch` and `ReadableStream`, reconnects after a broken stream, and handles `job_status`, `job_log`, `job_notified`, `setting_updated`, `push_gate_updated`, and `live_qr_gate_updated`. Vite proxies `/api` to backend port `8989` or `VITE_BACKEND_PORT` during frontend development.

`JobDetailPanel.vue` is implemented and tested as a component, but is not currently rendered by `App.vue`; do not describe it as part of the default visible screen.

## Runtime Data

- SQLite settings and job state: `backend/runtime/ideal_qr_tool.db` by default.
- Session bundles: `backend/runtime/session_cache/*.json`, with hashed filenames.
- QR artifacts: `backend/runtime/qr/*.png`.
- The UI input draft is the `ui.input_draft` settings key. Theme state is in memory.

## Verification Surface

- Root `npm test`: frontend Vitest, then backend `pytest -q`.
- Frontend `npm run build`: `vue-tsc --noEmit` then `vite build`.
- Backend pytest is configured with `testpaths = tests` and includes unit, property, and integration suites.
- `backend/test` contains additional scripts, some of which expect live credentials, external services, or a prepared runtime.

## Repository Caveats

- There is no backend README and no `.env.example`.
- Root metadata declares pnpm, but project scripts and setup use npm.
- `build.sh` and `build.bat` require absent `scripts/release/setup.sh` and `scripts/release/setup.bat` in this checkout.
- `backend/test/setup_e2e_settings.py` uses a separate E2E database convention from normal application startup.
