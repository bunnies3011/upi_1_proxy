# Project Overview And PDR

Verified against the source tree on 2026-07-12. This document describes the current product boundary and the requirements evidenced by the implementation; it is not a promise that every historical requirement is complete.

## Overview

iDEAL QR Tool is a single-operator web and CLI application for running a ChatGPT/Stripe/pay.ideal.nl iDEAL flow. It accepts account lines, schedules jobs, uses configurable proxies and device profiles, follows the payment redirect, and renders a QR PNG plus payment link when the flow succeeds.

The web process is a FastAPI application that can serve the built Vue UI from the same origin. SQLite stores settings and job state. Files under `backend/runtime/` hold session-cache JSON and QR artifacts.

## Product Development Requirements

| Area | Current requirement | Evidence |
| --- | --- | --- |
| Job intake | Accept account lines in the UI and CLI; report invalid or duplicate lines as skipped items. | `backend/app/api/routes_jobs.py`, `backend/app/cli/commands/run_batch.py` |
| Job lifecycle | Support pending, running, QR-ready, error, and stopped states plus held pending jobs. | `backend/app/core/payment_flow.py`, `backend/app/core/job_manager.py` |
| Payment flow | Run the registered `ideal` handler through ChatGPT, Stripe, and pay.ideal.nl, then render QR output. | `backend/app/payments/ideal/flow.py` |
| Runtime settings | Validate whitelisted `namespace.field` keys and persist updates atomically in SQLite. | `backend/app/core/settings_store.py` |
| Proxy operations | Store proxy configuration, lease proxies for jobs, and probe batches synchronously or as NDJSON. | `backend/app/core/proxy_pool.py`, `backend/app/api/routes_proxy.py` |
| Session reuse | Optionally reuse account sessions with a configurable TTL and explicit clear operations. | `backend/app/core/session_cache.py`, `backend/app/api/routes_session_cache.py` |
| Live operations | Broadcast job, settings, notification, and push-gate changes over one SSE stream. | `backend/app/core/sse.py`, `backend/app/api/routes_events.py` |
| Notifications | Configure Telegram push or pull mode and test/reset notification controls. | `backend/app/notifiers/telegram/`, `backend/app/api/routes_notifications.py` |
| Automation | Provide `ideal-qr run`, `run-batch`, `settings`, and `session-cache` commands with text or JSON output. | `backend/app/cli/main.py` |

## Non-Functional Requirements

- **Persistence:** SQLite is the authoritative store for settings and job records; filesystem artifacts are separate runtime data.
- **Lifecycle:** FastAPI startup fails when service bootstrap fails. Shutdown waits for job-manager tasks before closing SQLite.
- **Observability:** Job logs and status changes are available through REST and SSE; sensitive values are redacted in core logging paths.
- **Compatibility:** The backend targets Python 3.11 or newer. The frontend targets Node.js 18 or newer and uses Vue 3, TypeScript, Pinia, Naive UI, and Vite.
- **Operator ergonomics:** The UI is one responsive operations screen; the CLI can run without the UI.
- **Security boundary:** The current deployment assumes a trusted local network. API authentication is disabled, raw account lines are persisted and returned, and session-cache payloads may contain credentials.

## Acceptance Criteria For The Current Baseline

1. `bash setup.sh --skip-run` can install prerequisites, create the backend environment, install dependencies, build the frontend, and seed minimum settings when external package installation succeeds.
2. `./dev.sh` or `./production.sh` starts Uvicorn with the UI and API on the configured host and port when prerequisites are present.
3. `npm test` runs the frontend and backend test commands defined in the root package.
4. A successful job exposes a payment link and `image/png` QR endpoint; a non-ready job returns a QR-not-ready error.
5. Settings writes reject unknown keys and invalid values instead of silently accepting them.

## Constraints And Gaps

- The payment flow depends on external ChatGPT, Stripe, and pay.ideal.nl behavior, credentials, proxy quality, and anti-fraud controls. A local test pass does not prove a live payment will succeed.
- `frontend/src/components/JobDetailPanel.vue` exists, but `frontend/src/App.vue` currently renders `LogPanel` and `SuccessOutputPanel` instead of `JobDetailPanel`.
- The release bundle scripts reference absent `scripts/release` templates in this checkout.
- The historical [Telegram pull-mode review](./telegram-pull-job-mode_review.md) is preserved as internal evidence. Its dated test results were not rerun for this documentation pass.
