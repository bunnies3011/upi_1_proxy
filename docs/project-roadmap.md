# Project Roadmap

This roadmap is an evidence-based backlog from the verified 2026-07-12 baseline. It is not a dated delivery commitment.

## Current Baseline

- Web UI and CLI paths exist for iDEAL job execution.
- Payment methods: iDEAL, UPI (vendor), UPI no-CDK (OaiPay), **UPI Direct** (in-house ChatGPT+Stripe QR, 2026-07-14).
- SQLite-backed settings and jobs, session cache, QR artifacts, proxy pooling, SSE, and Telegram controls are wired.
- Unit, property, and integration tests exist for backend behavior; frontend tests are snapshot/property focused.
- Local, production-like, and Docker launch paths exist.
- JobManager uses per-method concurrency limiters and optional explicit proxy-lease health outcomes.

## Priority 0: Protect Deployments

1. Add an explicit authentication and authorization boundary before supporting non-loopback or multi-user deployments.
2. Decide whether raw `account_line` must remain in API responses. If not, replace the UI copy workflow with a safer short-lived or local-only mechanism.
3. Define file ownership and permission requirements for SQLite, session-cache JSON, QR artifacts, and backups. Add startup or deployment checks where appropriate.
4. Document credential rotation and cache invalidation procedures.

## Priority 1: Restore Release Confidence

1. Restore or replace the missing `scripts/release/setup.sh` and `scripts/release/setup.bat` templates, then verify `build.sh` and `build.bat` end to end.
2. Resolve the npm versus pnpm declaration mismatch and choose one supported installation contract.
3. Add a checked-in `.env.example` only after the supported environment-variable contract is finalized.
4. Add frontend lint and browser-level smoke coverage if the UI is a supported release surface.

## Priority 1: Verification Depth

1. Run the complete backend suite in a clean environment and record current failures separately from historical review results.
2. Add or maintain integration tests for startup, SSE reconnect, QR response semantics, and database persistence.
3. Keep live ChatGPT/Stripe/pay.ideal.nl checks isolated from deterministic CI; require explicit credentials and external-service availability.
4. Verify the Telegram pull-mode path end to end after its remaining wiring and test concerns are resolved.

## Priority 2: Product And Architecture

1. Decide whether `JobDetailPanel.vue` should replace or complement the currently rendered `LogPanel` and `SuccessOutputPanel`, then wire it only after the interaction contract is clear.
2. Introduce a router only if the product grows beyond the current single operations screen.
3. Add backup/restore tooling for SQLite and a documented cleanup policy for QR and session artifacts.
4. Add metrics or structured operational reporting for proxy health, job latency, retries, and external-service failures.

## Maintenance Rule

Every roadmap item that changes a command, endpoint, setting key, storage contract, security posture, or visible workflow should update the corresponding document and be verified against source and tests. Historical reports should remain immutable evidence, not be rewritten as current status.
