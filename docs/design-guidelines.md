# Frontend Design Guidelines

These guidelines document the current Vue UI conventions verified on 2026-07-12. They are for extending the existing operations console, not for inventing a separate marketing surface.

## Product Context

The target user repeatedly submits and monitors payment jobs. Optimize for scanning, stable layout, quick controls, and clear failure states. The current app has one operations screen and no router.

## Visual System

- Use Naive UI for controls and the Tabler icon wrappers from `frontend/src/icons.ts`.
- Use the existing theme tokens in `App.vue` instead of component-specific color literals.
- Preserve the magenta primary color `#cc0066` and its dark/light hover and pressed variants.
- Dark mode is the default. The light-mode toggle is in memory and is not a persisted user preference.
- Use compact typography, monospace treatment for identifiers/logs, and tabular numerals for counts and durations where the existing components do so.
- Use rounded tags and status pills for compact status indicators; keep action controls visually secondary to the job list and logs.

## Layout

The current desktop layout is a two-column grid: input, logs, and successful output stack on the left; the job list occupies the right column. At widths below 1100px it becomes a single-column layout, and below 767px action controls and job rows wrap for narrow screens. Preserve stable grid areas and avoid introducing page-level nested cards.

## Interaction Patterns

- Put settings, proxy, and Telegram configuration behind the existing topbar controls.
- Use familiar icons with titles for row actions such as stop, rerun, QR, copy, plan check, and remove.
- Use confirmation dialogs for destructive bulk actions. Keep toast messages for short success and failure feedback.
- Keep the selected job as the source for the visible log and success panels.
- Treat `job_status` as the current snapshot and `job_log` as an append-only display stream. Let the Pinia store reconcile both.
- Preserve the visible `Live`/`Reconnecting` SSE indicator and reconnect behavior.

## Data And Loading States

- Use the backend REST endpoints for initial and detail state; use SSE for live updates.
- Show QR output only after `qr_ready`; load PNG bytes through `/api/jobs/{job_id}/qr.png`.
- Keep proxy probe rows mapped to their input order even when NDJSON results arrive in completion order.
- Do not display raw credentials in newly added UI. The existing job API exposes `account_line`; treat that as a security debt, not a pattern to copy.

## Accessibility And Responsive Behavior

Preserve the existing `aria-label`, `aria-expanded`, `aria-live`, button title, and focus-visible patterns. Ensure labels and status text remain inside their containers at narrow widths. New controls should have a keyboard-accessible label and a clear disabled/loading state.

## Component Boundaries

- `App.vue`: page composition, global theme, SSE lifecycle, selected job.
- `useJobsStore`: job REST operations and event reconciliation.
- `useSettingsStore`: validated settings reads/writes and setting events.
- `useSse`: stream parsing, reconnect, and dispatch.
- `components/`: focused panels and modals.

`JobDetailPanel.vue` is available as a component but is not part of the current `App.vue` render tree. Any change that makes it visible should be documented as a workflow change and tested as such.
