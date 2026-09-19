# Code Standards

These standards describe patterns already present in the repository. They are intended to keep future changes compatible with the current boundaries rather than introduce a new framework.

## Structure And Ownership

- Keep HTTP concerns in `backend/app/api/`.
- Keep shared orchestration and persistence in `backend/app/core/`.
- Keep payment-specific behavior under `backend/app/payments/<method>/`.
- Keep notification-specific behavior under `backend/app/notifiers/`.
- Keep CLI parsing and command behavior in `backend/app/cli/`.
- Keep frontend page composition in `frontend/src/App.vue`, reusable UI in `components/`, and stateful API logic in `composables/`.
- Do not modify `docs/telegram-pull-job-mode_review.md` as part of routine documentation updates; it is historical/internal evidence.

## Backend Patterns

### Async Service Boundaries

FastAPI routes and service operations are asynchronous. Use the existing `DbEngine`/`aiosqlite` path for SQLite and close owned resources during lifespan or command cleanup. Startup is fail-fast; shutdown explicitly waits for job-manager tasks before closing the database.

### Settings

Settings keys use `namespace.field`. Namespaces register their own `TypeConstraint` entries with `SettingsRepository`. Validate the entire batch before writing and preserve atomic rollback semantics. Do not add an unvalidated JSON or YAML runtime configuration path.

### Jobs And Payment Methods

Routes should call the generic `JobManager` interface and should not import `payments.ideal` directly. A payment module registers its namespace and handler at the bootstrap boundary. Preserve job status transitions, cancellation, retry behavior, and persisted ordering when changing job logic.

### API Errors

Follow the existing stable `error_code`, `message`, and `details` shape for API failures. Use the route-specific status codes already established for missing jobs, invalid settings, conflicts, and unavailable QR artifacts. Do not make clients parse human-readable messages as identifiers.

### Sensitive Data

Use the existing redaction helpers for logs, SSE, proxy output, and user-facing diagnostic strings. Treat account lines, cookies, access tokens, passwords, and TOTP secrets as sensitive even when a field is technically present in an existing response schema. Do not add new raw credential output.

## Frontend Patterns

- Use Vue Composition API and TypeScript in single-file components.
- Use Pinia composables for shared job and settings state; avoid a second ad hoc state cache.
- Use Naive UI components and the existing Tabler icon wrappers rather than hand-drawn replacement icons.
- Use relative `/api` URLs so the same code works with the backend-served SPA and Vite development proxy.
- Keep SSE handling in `useSse`; merge event payloads through the stores instead of mutating unrelated components directly.
- Preserve the current no-router single-screen composition unless a real multi-view requirement is accepted.
- Use the existing theme tokens and responsive breakpoints. Avoid hardcoded component colors when a token exists.

## Testing

Add the narrowest test that proves changed behavior, then run the broader command when a shared contract changes:

```bash
cd frontend && npm test
cd ../backend && .venv/bin/python -m pytest -q
```

Backend tests cover unit, property, and integration behavior. Frontend tests currently cover snapshots and property-style component tests. The frontend package has no lint or E2E script, so a new quality gate must be added deliberately rather than assumed to exist.

## Documentation And Examples

Verify file paths, endpoint names, settings keys, and CLI flags against source before documenting them. Prefer relative links within `docs/`. Keep evergreen docs below 800 lines and the root README below 300 lines. Mark historical or not-rerun test results as such instead of presenting them as current verification.
