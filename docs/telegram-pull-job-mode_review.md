# Báo cáo Review — Spec `telegram-pull-job-mode`

> **Phạm vi**: Kiểm tra realtime các task đã đánh dấu hoàn thành `[x]`, đánh giá chất lượng code, phát hiện + fix gap/bug, soát code vs spec.
> **Người review**: Kiro (AI)
> **Lần 1 (review)**: 2026-07-07 — phát hiện gap.
> **Lần 2 (fix + verify + re-check)**: 2026-07-07 — đã fix GAP-1, GAP-2, ISSUE-3 và verify.
> **Commit gốc gần nhất**: `9edba0b feat: Add pull mode job assignment` (+ thay đổi chưa commit)

---

## 1. Cách kiểm tra (realtime) + kết quả sau fix

Chạy thực tế bằng venv (`.venv/bin/python -m pytest`), không dùng inline code:

| Nhóm test | Kết quả (sau fix) |
|---|---|
| Toàn bộ test Pull_Mode của task hoàn thành + boundary R20 (9 file + `test_architecture_core_boundary`) | ✅ **94 passed** |
| `test_job_manager_pull_mode.py` (gồm 2 test mới verify GAP-1/GAP-2) | ✅ **12 passed** |
| `test_job_manager.py` (test lõi cũ — đã fix stale doubles ISSUE-3) | ✅ **18 passed** (trước: 9 failed) |
| Toàn bộ `tests/unit` + `tests/property` | ⚠️ **561 passed / 11 failed** — 11 lỗi là **tech-debt có sẵn**, KHÔNG do Pull_Mode (xem §5.4) |

---

## 2. Trạng thái task theo `tasks.md`

| Trạng thái | Task |
|---|---|
| ✅ Hoàn thành `[x]` | 1–28, 30 |
| 🔶 Đang làm `[~]` | 31, 32, 33, 34, 35, 36 |
| ⬜ Chưa làm `[-]` | 29 (`PullJobNotifier`) |

**Lưu ý**: task 27/28/30 đã đánh `[x]` nhưng **test tương ứng (task 31) chưa làm** — xem GAP-7 §6b.

---

## 3. Đánh giá chất lượng code (task hoàn thành)

Tổng thể **chất lượng cao, bám sát design.md**. Tách trách nhiệm rõ (SOLID), docstring trace requirement, xử lý edge case + concurrency cẩn thận.

- **`db.py` (task 1)**: migration idempotent per-column (`PRAGMA table_info` check từng cột), bảng `telegram_worker_stats` chuẩn schema §4.2.
- **`job_repo.py` (task 3)**: đọc/ghi 10 field mới phòng thủ theo index; `upsert_worker_stat_delta` **atomic** + `COALESCE` chống lost-update (R22.12); `list_worker_stats` sort sẵn R16.1.
- **Settings namespace (task 5)**: migration key cũ raw SQL trước whitelist, idempotent, fail-fast I/O; seed 8 key default không đè giá trị đã có.
- **`notifier.py` (task 6)**: đổi sang `push_mode.*`; early-return job Pull_Mode (R1.6).
- **`client.py` (task 20)**: tách sending/polling session; surface HTTP 409; parse list vs dict riêng.
- **`formatter.build_pull_caption` (task 22)**: escape HTML, ưu tiên `@username`, omit worker nếu rỗng.
- **`polling.py` (task 23)**: dedupe LRU 500; persist offset trước vòng kế; 409 clean-exit + suppress-respawn; swallow per-update.
- **`commands.py` (task 25)**: regex chuẩn R19.5; check `allowed_chat_ids`; `/stats` escape + giới hạn 20 dòng; `/reset` gắn `requester_id`.
- **`JobManager` methods (task 8–16)**: `claim_pull_account` atomic dưới `self._lock`; `resolve_pull_outcome` lock per-job + absorbing (R22.7/R22.11); `record_plus_check_transition` whitelist + CAS chống race (R22.9); hook pattern giữ boundary R20 (test pass).

---

## 4. Điểm tốt nổi bật

- Concurrency nghiêm túc: lock chung cho pool, lock per-job cho outcome/transition, atomic SQL cho counter, có test race thực (`asyncio.gather`).
- Fail-fast vs best-effort phân định rõ ràng.
- Boundary R20 giữ đúng (dùng hook thay vì import ngược) — verify bằng test.

---

## 5. Gap & Bug

### ✅ GAP-1 — [ĐÃ FIX] Job Pull_Mode lỗi `proxy_exhausted` không được resolve (R13.1)
**Trước**: nhánh `ProxyExhaustedError` trong `_run_handler` set ERROR rồi `return` ngay, không gọi `resolve_pull_error` cho job Pull_Mode đã ASSIGNED → `pull_outcome` kẹt `PENDING` vĩnh viễn, worker bị chiếm slot, không ghi fail, không thông báo.

**Fix**: thêm nhánh — nếu `pull_assignment_state == ASSIGNED` thì gọi `resolve_pull_error(job_id, "proxy_exhausted")` trước `return`. **Giữ nguyên 100% hành vi Push_Mode** (không thêm auto-retry cho push, đúng như cũ).

**Verify**: `test_proxy_exhausted_auto_resolves_pull_outcome_fail` (pool raise exhausted + `fallback_direct_on_exhausted=False`) → assert `pull_outcome == FAIL`, `error_code == "proxy_exhausted"`. PASS.

### ✅ GAP-2 — [ĐÃ FIX] Job Pull_Mode lỗi `internal_error` (exception bất ngờ) không được resolve (R13.1)
**Trước**: block `except Exception` gọi `_maybe_auto_retry(record)` vô điều kiện; `internal_error` không thuộc whitelist retry → no-op → `pull_outcome` kẹt `PENDING`.

**Fix**: tạo helper dùng chung `_dispatch_error_outcome(record, error_code)` — job Pull_Mode ASSIGNED → `resolve_pull_error`, còn lại → `_maybe_auto_retry` (giữ nguyên hành vi Push_Mode). Áp dụng cho **cả 3 nhánh ERROR** (proxy_exhausted xử lý riêng để không đổi push, handler-return-ERROR, except-Exception).

**Verify**: `test_unexpected_exception_auto_resolves_pull_outcome_fail` (handler raise `RuntimeError`) → assert `pull_outcome == FAIL`, `error_code == "internal_error"`. PASS.

> **Tóm tắt fix code** (`backend/app/core/job_manager.py`):
> - Thêm method `_dispatch_error_outcome` (định tuyến ERROR → resolve_pull_error vs _maybe_auto_retry).
> - Nhánh main ERROR + nhánh `except Exception`: dùng helper.
> - Nhánh `proxy_exhausted`: thêm resolve pull riêng, giữ push nguyên trạng.

### ✅ ISSUE-3 — [ĐÃ FIX] 9 test cũ `test_job_manager.py` fail do stale doubles
**Nguyên nhân**: `FakeProxyPool.acquire()` thiếu kwarg `wait_for_release`/`cancellation_token` (feature proxy-health, commit `da1356d`, trước Pull_Mode); `FakeSettings` thiếu `.get()`; `test_scheduler_proxy_exhausted_marks_error` cần `probe_config.fallback_direct_on_exhausted=False` mới đi đúng nhánh ERROR (default proxy-health là `True`).

**Fix** (`backend/tests/unit/test_job_manager.py`): bổ sung `FakeSettings.get()`; `FakeProxyPool.acquire(*, wait_for_release, cancellation_token)`; `FakeProxyPool.probe_config = ProbeConfig(enabled=False, fallback_direct_on_exhausted=False)`. → **18 passed**.

### 🟡 ISSUE-6 — 11 test FAIL còn lại ở suite rộng: TECH-DEBT CÓ SẴN, KHÔNG do Pull_Mode
Đã xác minh **không phải** do Pull_Mode và **không** do fix lần này (các file đó không bị đụng, và thay đổi `job_manager.py` là trung tính cho job non-pull):

| File test fail | Root cause (feature nguồn, đều predate/độc lập Pull_Mode) |
|---|---|
| `test_log_entries_required_fields.py` (3), `test_hide_job_preserves_backend.py` (2), `test_job_manager_concurrency_property.py` (1) | `FakeProxyPool.acquire()` riêng của các file này thiếu `wait_for_release` (proxy-health) |
| `test_settings_whitelist_full.py::...covers_exactly_16_keys` (24≠16) | Core keys tăng do namespace `ui` + 7 "probe knobs" proxy-health — test chưa cập nhật số |
| `test_log_entries_complete_fields.py` (1) | `_RecordingLogger.info()` sai chữ ký (feature logging) |
| `test_flow_qr_ready_property.py` (1) | `_FakeStripeClient` thiếu `ensure_token_config` (feature stripe token) |
| `test_sse.py` (1), `test_startup_smoke.py` (1) | Assertion payload/startup của feature khác |

**Khuyến nghị**: cập nhật các stale double này trong 1 đợt dọn tech-debt riêng (ngoài phạm vi Pull_Mode). Task 36 chỉ nên yêu cầu xanh các test liên quan Pull_Mode + không phá test do CHÍNH Pull_Mode gây ra (hiện đã đạt).

### 🟡 NOTE — Quan sát thêm (chưa phải bug, ngoài scope task hoàn thành)
- **Stop job Pull_Mode đã ASSIGNED giữa chừng qua web-UI**: handler bị override thành STOPPED → không QR_READY, không resolve → `pull_outcome` giữ `PENDING`, slot worker vẫn bị tính. Spec chưa định nghĩa hành vi này (R4.5 chỉ nói job `unassigned`). Cần làm rõ khi triển khai task còn lại.
- Cảnh báo asyncio "Task was destroyed but pending" (scheduler/cleanup loop chưa dừng trong 1 số test) — nên có fixture gọi `shutdown()`.

---

## 6. Soát code vs spec lần 2 (task hoàn thành) — KẾT LUẬN

Đã đối chiếu lại code với requirements/design cho toàn bộ task `[x]`:

- **R2 (Settings namespace)**: 8 key + constraint (type/min/max/enum/default) khớp §4.3. ✅
- **R4.1/4.6 (submit_batch/load_from_db)**: nhánh pull không vào `_pending_order`; load filter `unassigned`. ✅ (đọc mode 1 lần/batch — deviation hợp lý, nhất quán hơn, đã document)
- **R5.6/5.8, R6, R22.1/22.2/22.10 (claim)**: atomic count-check-dequeue-assign dưới lock. ✅
- **R13.1/13.5 (ERROR → FAIL)**: sau fix, **cả 3 nhánh ERROR** đều chốt FAIL cho job Pull_Mode ASSIGNED. ✅ (trước fix: chỉ 1/3)
- **R15/16/17 (worker stats)**: atomic delta + COALESCE, sort ranking, reset. ✅
- **R20 (boundary)**: `core/` không import `notifiers.*` — test boundary pass. ✅
- **R22.7/22.9/22.11 (absorbing outcome, race transition/outcome)**: lock per-job + absorbing guard + CAS. ✅

**Không phát hiện gap mới** ngoài GAP-1/GAP-2 (đã fix) trong phạm vi task hoàn thành.

---

## 6b. Soát task mới done: 27, 28, 30 (`pull_mode.py`, `callback_router.py`)

Vòng review bổ sung sau khi task 27 (PullJobCoordinator), 28 (reset_confirm/cancel), 30 (Callback_Router) được đánh `[x]`.

**Code — KHÔNG có bug, bám sát spec:**
- `_handle_claim` (R5.2–5.7): check chat → mode → `claim_pull_account` → phân biệt lý do `None` (limit vs pool rỗng) → answer. Đúng.
- `_handle_done` (R9, R10, R11): check tồn tại → ownership → terminal → guard "checking" → `armed→checking` (CAS) → `check_plan_status` → plus/retry/exhausted; exception `check_plan_status` tự phục hồi state (R10.7), không kẹt "checking". Đúng.
- `_handle_fail` (R9, R12): override cả `armed`/`checking` (R12.4), KHÔNG gọi `check_plan_status` (R12.2), race → "đã có kết quả". Đúng.
- `_handle_reset_confirm/cancel` (R17): dedup theo `chat_id:message_id`, ownership `requester_id`, confirm gọi `reset_all_worker_stats` fail-fast. Đúng.
- `callback_router` (R18.1–18.3): dispatch `plus_check:` (stub) / `pull_job:` / fallback ACK rỗng. Đúng.
- Race R22.11: CAS transition + absorbing `pull_outcome` đảm bảo done/fail đồng thời chỉ 1 thắng — logic đúng.
- Boundary R20: chỉ gọi method public, không mutate `_JobRecord`. Đúng.
- Tích hợp `check_plan_status`: handler iDEAL (`flow.py`) CÓ implement, trả dict `{plan: plus|free|unknown}` → `result.get("plan") == "plus"` chạy đúng.
- Import + init sạch (đã smoke-check).

**GAP thực sự:**

### 🔴 GAP-7 — Task 27/28/30 done nhưng CHƯA có unit test (task 31 `[~]`)
`test_pull_job_coordinator.py` / `test_callback_router.py` / `test_pull_job_notifier.py` **chưa tồn tại**. `pull_mode.py` (~777 dòng, logic race phức tạp R22.11) hoàn toàn **chưa được verify tự động**. Vi phạm quy tắc TDD tối thiểu của project (mỗi task code có task test đi kèm). → Nên hoàn thành task 31 trước khi coi 27/28/30 thực sự "done".

### 🟠 GAP-8 — Chưa chạy được end-to-end (phụ thuộc task chưa xong)
- Task 29 (`PullJobNotifier`) `[-]`: chưa gửi QR + chưa gọi `initialize_pull_verify_state` → `telegram_message_chat_id/id` luôn `None` (nhánh gỡ nút trong done/fail bị skip), pull_error hook chưa đăng ký. Done/Fail chưa thể trigger thật.
- Task 32 (wiring `bootstrap.py`) `[~]`: coordinator/router chưa gắn vào `PollingSupervisor` → chưa chạy trong production.

Đây là **sequencing bình thường** (không phải bug), nhưng nghĩa là 27/28/30 tuy "code xong" vẫn chưa hoạt động cho tới khi 29 + 32 hoàn thành.

---

## 7. Việc còn lại

1. **Ưu tiên**: hoàn thành task 31 (unit test cho `pull_mode.py`/`callback_router.py`, gồm test race R22.11) — GAP-7. Code 27/28/30 hiện chưa verify tự động.
2. Hoàn thành task 29 (`PullJobNotifier`) + 32 (wiring bootstrap) để luồng Pull_Mode chạy end-to-end — GAP-8.
3. Hoàn thành task 33–35 (endpoint API mode-switch, property test) + 36 (regression toàn suite).
4. Dọn tech-debt stale test doubles (ISSUE-6) trong đợt riêng.
5. Làm rõ hành vi stop job Pull_Mode đã ASSIGNED (NOTE §5).
6. Dọn cảnh báo asyncio task pending trong test.

---

*File cập nhật khi có thay đổi mới. Lịch sử: L1 phát hiện gap → L2 fix GAP-1/GAP-2/ISSUE-3 + verify + soát spec.*
