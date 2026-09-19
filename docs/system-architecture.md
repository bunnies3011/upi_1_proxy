# System Architecture

Verified against the current source tree on 2026-07-12.

## Runtime Topology

```text
Browser / CLI
     |
     | REST, SSE, or CLI service calls
     v
FastAPI app (`backend/app/main.py`)
     |
     +-- API routers: jobs, settings, proxy, session-cache, notifications, events
     |
     +-- Bootstrap services (`backend/app/bootstrap.py`)
     |      +-- SQLite / SettingsRepository
     |      +-- JobManager / JobRepository
     |      +-- ProxyPool
     |      +-- AccountSessionCache
     |      +-- SseBroadcaster
     |      +-- Telegram notifier and polling supervisor
     |      +-- registered IdealFlowHandler and UpiFlowHandler
     |      +-- UpiLicensePool (PK-XXXX license credit pool)
     |
     +-- static `frontend/dist/` when a frontend build exists
     |
     +-- external HTTP services
            iDEAL: ChatGPT -> Stripe -> pay.ideal.nl / iDEAL transaction API
            UPI:   ChatGPT login -> pix.capybara.cv vendor (UPI QR)

Runtime files:
  SQLite: backend/runtime/*.db
  Sessions: backend/runtime/session_cache/*.json
  QR PNG: backend/runtime/qr/*.png
```

The CLI reuses `bootstrap_services` without FastAPI dependency injection. The web app configures API dependencies during lifespan startup.

## Lifecycle

1. `create_app()` resolves the database path, bind-host metadata, session-cache directory, and QR directory.
2. FastAPI lifespan calls `bootstrap_services`, which initializes the database and service singletons, registers settings namespaces (including `ideal.*`, `upi.*`, `oaipay.*`, and `upi_direct.*`), hydrates proxy/session and UPI license-pool state, and registers the iDEAL, UPI, UPI no-CDK (`upi_nocdk`), and UPI Direct (`upi_direct`) handlers.
3. API dependencies and application state are configured, then the process serves REST, SSE, and optionally the built SPA.
4. On shutdown, the job manager is shut down first, Telegram HTTP/polling resources are closed, and the SQLite engine is closed last.

## Core Boundaries

### API To Core

API routes depend on generic core services. Job routes submit, list, stop, rerun, delete, check plans, and serve QR PNGs. Settings routes enforce the special Telegram mode switch path. Proxy routes expose batch probe results and streaming NDJSON. The events route forwards the broadcaster queue as `text/event-stream`.

### Core To Payment Module

`JobManager` stores generic jobs and invokes a registered handler. Four payment packages supply handlers and namespace registration through the bootstrap integration point: `payments/ideal` (`IdealFlowHandler`), `payments/upi` (`UpiFlowHandler`), `payments/upi_oaipay` (`OaipayFlowHandler`, method key `upi_nocdk`), and `payments/upi_direct` (`UpiDirectFlowHandler`, method key `upi_direct`). All reuse the shared ChatGPT login/session surface in `payments/_chatgpt` (login client, session resolution, account parsing); iDEAL extends it with Stripe checkout, UPI-CDK takes the `access_token` and calls the license-gated `pix.capybara.cv` vendor, UPI no-CDK takes the same `access_token` and calls OaiPay `long-link-stream` (mode 5 UPI) with YesCaptcha Turnstile — **no license pool**, and UPI Direct drives ChatGPT+Stripe UPI QR in-house with dual proxy pools A/B and **no third-party payment vendor**. Concurrency is **per payment method** via method-keyed limiters (not a global last-write-wins semaphore). Handlers may set `JobResult.proxy_lease_health` for explicit core-lease attribution; legacy handlers keep final-result inference. This keeps `core/` independent of the concrete payment method. The UPI-CDK vendor call sends the ChatGPT `access_token` to a third-party host; see [UPI vendor security](./upi-vendor-security.md). UPI Direct egress/proxy roles: [UPI Direct security](./upi-direct-security.md).

**UPI no-CDK egress model (two clients):** ChatGPT login uses the job `proxy_lease` (proxied); OaiPay endpoints + YesCaptcha egress un-proxied (server IP). Operator proxies travel only in the request body `proxyPools` (checkout + promotion). Concurrency (`oaipay.max_concurrent`) feeds the **shared global** JobManager semaphore (last-write-wins across methods — same as UPI-CDK).

**Secret posture:** `oaipay.yescaptcha_api_key` is a plain setting (settings API has no auth, trusted-LAN). It is protected from log/SSE/error leakage via `known_secrets` value-redaction + the `redact_dict` field-name backstop (`api_key` / `yescaptcha_api_key`), not masked in the settings API/CLI. `oaipay.base_url` is https + host-allowlisted because it receives the live access token.

### Frontend To API

`App.vue` composes the current operations screen. Pinia stores use relative REST paths. `useSse` reads the stream using a browser `ReadableStream`, ignores keepalive comments, reconnects after failure, and dispatches known event types to stores.

## Job Flow

```text
account lines
  -> parse and deduplicate
  -> pending or pending+held
  -> scheduler / explicit start
  -> proxy lease and session resolution
  -> ChatGPT login or cached session
  -> checkout and Stripe setup/confirmation
  -> ChatGPT approval and Stripe redirect polling
  -> pay.ideal.nl transaction initiation
  -> QR PNG render + payment link
  -> qr_ready, error, or stopped
```

The flow also exposes a plan-check path that uses the cached ChatGPT session when available.

## API Surface

The implemented route groups are:

| Prefix | Responsibility |
| --- | --- |
| `/api/jobs` | Submit, list, inspect, start, stop, rerun, remove, clear, plan-check, QR PNG |
| `/api/settings` | Read and write validated settings; guarded Telegram mode switch |
| `/api/session-cache` | Clear one account cache or all cache files |
| `/api/proxy` | Probe a proxy batch as one response or NDJSON stream |
| `/api/notifications` | Telegram test/reset, push-gate (success wait), and live-qr-gate snapshot |
| `/api/events/stream` | SSE status, log, setting, notification, push-gate, and live-qr-gate events |

There is no API authentication in the current implementation.

## Persistence And Consistency

SQLite is the source of truth for job and settings state. Settings updates validate keys and values before atomic writes. Session cache uses hashed filenames and atomic temporary-file replacement, but the payload itself may contain cookies or tokens. QR PNG files are separate artifacts referenced by job state and served through the QR endpoint.

## Telegram Push capacity modes

Two mutually exclusive Push_Mode capacity controls (settings mutex auto-clears the other mode on both single-key PUT and bulk):

1. **Success Wait** (`telegram.push_mode.success_wait.*`) — after N successful Telegram sends, pause scheduler + worker until the operator presses Resume.
2. **Live QR Gate** (`telegram.push_mode.live_qr.*`) — per-chat cap on concurrent "living" QR messages (default `max_per_chat=5`). Slot key is `job_id:message_id`. Free a slot on Plus verified, 5m plan-check timeout, job lifecycle end (stop/rerun/delete/error), or a TTL sweep backstop (~330s). Freeing a slot auto-wakes scheduler and worker — **no Resume button**.

Implementation: in-memory `LiveQrGate` (`notifiers/telegram/live_qr_gate.py`), wired through notifier acquire/release, composite `register_push_pause_checker`, SSE `live_qr_gate_updated`, and FE `useLiveQrGate`.

**Operator warnings:**

- **Restart flood (accepted):** the live counter is in-memory. After process restart, QRs still live on Telegram are not counted, so for one TTL window the gate can allow up to ~2× the configured cap.
- **Pause resume-from-cache:** when Live Gate (or Success Wait) pauses a RUNNING Push job, re-run hits the session cache and regenerates the QR — it does **not** re-login from scratch.

### Batch Plus tally (DB-backed)

Push-mode Plus/expired outcomes keep a per-chat payroll tally across restarts:

| Piece | Storage | Role |
| --- | --- | --- |
| Counts + tally message id | `telegram_batch_tally` | `#N` tag + single editable tally message per chat |
| Outcome dedup | `jobs.plan_outcome_notified` (`0`/`1`/`2`) | One count per job per period; rerun defers re-arm until close |
| Atomic claim+count | `JobRepository.try_claim_and_increment_tally` | Commit before Telegram tag send (no double-count, no claimed-but-uncounted) |

The claim+increment runs on a **dedicated `DbEngine` write connection** serialized by `claim_lock`, so its transaction is isolated from the process-wide shared connection — no other coroutine's `commit()`/`rollback()` can split or undo the claim (the atomicity guarantee holds at runtime, not just on the happy path).

**Period close** (`TelegramNotifier.reset_batch_tally`): snapshot → permanent receipt → decrement by receipted amount (not absolute zero). Failed receipts stay open and appear in `skipped`. Deferred flags re-arm once after the loop. Triggers: web `POST .../batch-tally/reset` and gated `/chotky`. No history table — the receipt message is the record. Tally path never writes `jobs.plan`.

Edge case (intentional, per "no same-period recount"): an account counted as **expired** that is then rerun and verifies **Plus in the same period** is not recounted as Plus that period — the rerun defers its flag (`1`→`2`) and re-arms only at the next period close.

### Free-export panel (frontend-only)

`FreeOutputPanel` uses `jobsStore.freeJobs` (visible non-Plus by plan only). Reuses `GET /api/jobs` / SSE — no free-export API.

## Security Boundary

The code redacts many logs and masks proxy output, but this is not an authenticated multi-user service. Raw `account_line` values are currently returned by job views and can contain credentials. Deployments that bind beyond loopback need an external access-control layer, protected runtime storage, and an explicit review of backup and log handling.

The UPI-CDK payment method adds a third-party egress posture: the ChatGPT `access_token` leaves the system to `pix.capybara.cv`. The token value is added to the per-job redaction secret set before the first vendor call, and the raw vendor request body is never logged. See [UPI vendor security](./upi-vendor-security.md) for the full data-egress, redaction, blast-radius, and license-credit analysis.

UPI no-CDK (`upi_nocdk`) similarly sends the access token to OaiPay (`oaipay.base_url`, default `https://oaipay.12001234.xyz`) in the long-link-stream body, with the same value-redaction discipline; captcha solver traffic stays on the server IP.

## Historical Evidence

The [Telegram pull-mode review](./telegram-pull-job-mode_review.md) records historical implementation findings and dated test results. It is preserved unchanged and was not freshly rerun as part of this architecture documentation pass.
