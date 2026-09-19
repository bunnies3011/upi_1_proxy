"""Property-based state-machine test cho 12 invariant Pull_Mode
(Requirement 22, spec `telegram-pull-job-mode`, task 35).

Mô hình hoá vòng đời Pull_Mode bằng `hypothesis.stateful.RuleBasedStateMachine`
với 5 rule mô phỏng đúng các đường tương tác thật lên `JobManager`:

  - ``claim(worker)``        — worker bấm Nhận_Job_Button → ``claim_pull_account``.
  - ``done(job, plan)``      — Callback_Handler chạy 1 chu kỳ verify Plus qua
                               ``record_plus_check_transition`` rồi
                               ``resolve_pull_outcome(SUCCESS)`` khi Plus xác
                               nhận / ``FAIL`` khi hết 2 lượt.
  - ``fail(job)``            — worker bấm Thất_Bại_Button → ``resolve_pull_outcome(FAIL)``.
  - ``simulate_error(job)``  — job ASSIGNED chuyển ERROR kỹ thuật →
                               ``resolve_pull_error`` → ``resolve_pull_outcome(FAIL)``.
  - ``switch_mode(force)``   — ``set_operating_mode`` (Mode_Switch_Guard).

Sau MỖI rule, `hypothesis` chạy toàn bộ `@invariant` bên dưới để kiểm 8 bất
biến (Requirement 22.1–22.8):

  22.1  Không job nào đổi ``pull_assigned_telegram_user_id`` theo thời gian.
  22.2  Không worker nào vượt limit hiện hành (``max_concurrent_jobs_per_user``).
  22.3  ``success_count + fail_count`` mỗi worker đơn điệu không giảm.
  22.4  Mỗi job chỉ được tính công (resolve) đúng 1 lần.
  22.5  Không account nào bị claim 2 lần (mỗi account gán tối đa 1 worker).
  22.6  ``claim_pull_account`` trả account theo đúng thứ tự FIFO (order tăng dần).
  22.7  ``pull_outcome`` là absorbing — SUCCESS/FAIL đã chốt không bao giờ đổi.
  22.8  ``plus_check_attempts <= 2`` với mọi record ở mọi thời điểm.

Cross-reference (KHÔNG lặp lại ở file này — đã có property coverage nơi khác):
  - 22.9  (CAS chống race verify 2 lần trên cùng 1 job) — task 17
          (``record_plus_check_transition`` lock per-job).
  - 22.10 (đúng 1 worker thắng race claim cùng account) — task 15
          (``tests/unit/test_job_manager_pull_mode.py`` race tests).
  - 22.11 (đúng 1 đường thắng race resolve cùng 1 job) — task 31.
  - 22.12 (``record_worker_stat_delta`` atomic ở tầng SQL) — task 4
          (``JobRepository.upsert_worker_stat_delta`` ON CONFLICT).

Test double được TÁI DÙNG (import, không duplicate) từ:
  - ``tests/unit/test_job_manager.py`` (``FakeHandler``, ``FakeSse``).
  - ``tests/unit/test_job_manager_pull_mode.py`` (``FakeJobRepo``, ``_seed_accounts``).
  - ``tests/unit/test_job_manager_mode_switch.py`` (``_make_mode_switch_manager``,
    ``_ModeSwitchFakeSettings`` — bản Settings duy nhất có cả ``.get/.list/.set``).

Async-in-hypothesis: rule của `RuleBasedStateMachine` là hàm SYNC, nên state
machine tự giữ 1 event loop bền (``asyncio.new_event_loop``) suốt vòng đời 1
instance và dùng ``loop.run_until_complete`` cho mỗi lời gọi async của
`JobManager` — nhất quán với cách các property test khác trong repo chạy
async qua ``asyncio.run`` (VD ``test_job_manager_concurrency_property.py``),
chỉ khác là loop được tái dùng qua nhiều rule thay vì tạo mới mỗi lần. Profile
`hypothesis` "fast" (``max_examples=20``) được load global ở
``tests/conftest.py``; ``deadline=None`` vì mỗi rule có task nền của scheduler.

**Validates: Requirements 22.1, 22.2, 22.3, 22.4, 22.5, 22.6, 22.7, 22.8**
"""

from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import settings as hyp_settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from app.core.errors import (
    JobAlreadyResolvedError,
    JobNotFoundError,
    ModeSwitchBlockedError,
)
from app.core.job_manager import (
    PullAssignmentState,
    PullOutcome,
)
from app.core.payment_flow import JobResult, JobStatus

from tests.unit.test_job_manager import FakeHandler
from tests.unit.test_job_manager_mode_switch import _make_mode_switch_manager
from tests.unit.test_job_manager_pull_mode import FakeJobRepo, _seed_accounts

# ---------------------------------------------------------------------------
# Tham số mô hình — giữ nhỏ để mỗi example chạy trong vài ms (fast profile).
# ---------------------------------------------------------------------------
_NUM_ACCOUNTS = 5
_WORKERS = ("worker-1", "worker-2", "worker-3")
_LIMIT_PER_WORKER = 2


class PullModeInvariantsMachine(RuleBasedStateMachine):
    """State machine kiểm 8 invariant Pull_Mode (Requirement 22.1–22.8).

    Duy trì 1 "shadow model" song song `JobManager` để phát hiện vi phạm:
      - ``_first_worker``   : job_id -> worker_id đầu tiên đã gán (22.1, 22.5).
      - ``_resolved``       : job_id -> PullOutcome đã chốt (22.4, 22.7).
      - ``_prev_worker_totals`` : worker -> (success+fail) lần kiểm trước (22.3).
    """

    def __init__(self) -> None:
        super().__init__()
        # Event loop bền cho cả vòng đời instance — mọi async call của
        # JobManager (kể cả task nền scheduler) chạy trên loop này.
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self.manager, self.settings, _, _ = _make_mode_switch_manager(
            max_concurrent_per_user=_LIMIT_PER_WORKER
        )
        # Gắn FakeJobRepo để `resolve_pull_outcome` -> `record_worker_stat_delta`
        # thực sự ghi delta (nguồn dữ liệu kiểm 22.3). Pattern gắn trực tiếp
        # attribute private theo đúng các test khác trong suite.
        self.repo = FakeJobRepo()
        self.manager._job_repo = self.repo  # noqa: SLF001

        self.manager.register_handler(
            "ideal", FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
        )
        # Seed Pull_Account_Pool: N account UNASSIGNED (mode mặc định "pull").
        self._job_ids: list[str] = self._run(
            _seed_accounts(self.manager, _NUM_ACCOUNTS)
        )

        # Shadow model.
        self._first_worker: dict[str, str] = {}
        self._resolved: dict[str, PullOutcome] = {}
        self._prev_worker_totals: dict[str, int] = {w: 0 for w in _WORKERS}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _run(self, coro) -> Any:
        """Chạy 1 coroutine tới hoàn tất trên loop bền của instance."""
        return self._loop.run_until_complete(coro)

    def _pending_assigned(self, job_id: str):
        """Trả record nếu job đang ASSIGNED + pull_outcome PENDING, else None."""
        record = self.manager.get_job(job_id)
        if record is None:
            return None
        if (
            record.pull_assignment_state == PullAssignmentState.ASSIGNED
            and record.pull_outcome == PullOutcome.PENDING
        ):
            return record
        return None

    def _fifo_head_job_id(self) -> str | None:
        """job_id UNASSIGNED có ``order`` nhỏ nhất (đầu FIFO), hoặc None."""
        unassigned = [
            r
            for r in self.manager.list_jobs()
            if r.pull_assignment_state == PullAssignmentState.UNASSIGNED
        ]
        if not unassigned:
            return None
        return min(unassigned, key=lambda r: r.order).job.job_id

    def _mark_resolved(self, job_id: str, outcome: PullOutcome) -> None:
        """Ghi shadow khi 1 resolve thành công — dùng chung cho mọi đường
        (done/fail/simulate_error/switch_mode). Phát hiện double-count
        (22.4) ngay tại đây: 1 job KHÔNG được ghi resolved 2 lần."""
        assert job_id not in self._resolved, (
            f"22.4 vi phạm: job {job_id} bị tính công 2 lần "
            f"(cũ={self._resolved[job_id]}, mới={outcome})"
        )
        self._resolved[job_id] = outcome

    # ------------------------------------------------------------------
    # Rule: claim
    # ------------------------------------------------------------------
    @rule(worker_idx=st.integers(min_value=0, max_value=len(_WORKERS) - 1))
    def claim(self, worker_idx: int) -> None:
        worker = _WORKERS[worker_idx]

        # Tính kỳ vọng NGAY TRƯỚC claim (state đọc dưới cùng 1 coroutine
        # tuần tự — không có coroutine khác xen giữa).
        active_before = self.manager.get_worker_active_pull_count(worker)
        expected_head = self._fifo_head_job_id()
        expected_can_claim = active_before < _LIMIT_PER_WORKER and expected_head is not None

        claimed = self._run(
            self.manager.claim_pull_account(worker, f"chat-{worker}", worker, None)
        )

        if claimed is None:
            # Chỉ được trả None khi hết capacity worker HOẶC pool rỗng.
            assert not expected_can_claim, (
                "claim trả None nhưng lẽ ra phải thành công "
                f"(worker={worker}, active={active_before}, "
                f"limit={_LIMIT_PER_WORKER}, head={expected_head})"
            )
            return

        # (22.6) FIFO: account trả về phải đúng đầu hàng đợi.
        assert claimed == expected_head, (
            f"22.6 FIFO vi phạm: claim trả {claimed} nhưng đầu FIFO là "
            f"{expected_head}"
        )
        # (22.5) Không account nào bị claim 2 lần.
        assert claimed not in self._first_worker, (
            f"22.5 vi phạm: account {claimed} bị claim lần 2 "
            f"(worker cũ={self._first_worker.get(claimed)}, mới={worker})"
        )
        self._first_worker[claimed] = worker

        # Sau claim, job phải ASSIGNED cho đúng worker này + PENDING.
        record = self.manager.get_job(claimed)
        assert record is not None
        assert record.pull_assignment_state == PullAssignmentState.ASSIGNED
        assert record.pull_assigned_telegram_user_id == worker
        assert record.pull_outcome == PullOutcome.PENDING

    # ------------------------------------------------------------------
    # Rule: done — chu kỳ verify Plus rồi resolve
    # ------------------------------------------------------------------
    @rule(
        job_idx=st.integers(min_value=0, max_value=_NUM_ACCOUNTS - 1),
        plus_detected=st.booleans(),
    )
    def done(self, job_idx: int, plus_detected: bool) -> None:
        job_id = self._job_ids[job_idx]
        record = self._pending_assigned(job_id)
        if record is None:
            return  # job chưa được claim / đã resolve — no-op hợp lệ.

        state = record.plus_check_state.value

        # Chỉ chạy chu kỳ verify khi state cho phép bắt đầu/tiếp tục check.
        if state == "armed":
            # Callback_Handler: tăng attempts NGAY TRƯỚC khi check plan.
            attempts_now = record.plus_check_attempts
            if not self._run(
                self.manager.record_plus_check_transition(
                    job_id, "armed", "checking", attempts_now
                )
            ):
                return
            record = self.manager.get_job(job_id)
            assert record is not None
            state = "checking"

        if state != "checking":
            return  # verified / exhausted_2 / failed_marked — đã kết thúc chu kỳ.

        attempts_now = record.plus_check_attempts
        if plus_detected:
            ok = self._run(
                self.manager.record_plus_check_transition(
                    job_id, "checking", "verified", attempts_now
                )
            )
            if ok:
                self._run(
                    self.manager.resolve_pull_outcome(job_id, PullOutcome.SUCCESS)
                )
                self._mark_resolved(job_id, PullOutcome.SUCCESS)
        elif attempts_now >= 2:
            ok = self._run(
                self.manager.record_plus_check_transition(
                    job_id, "checking", "exhausted_2", attempts_now
                )
            )
            if ok:
                self._run(
                    self.manager.resolve_pull_outcome(job_id, PullOutcome.FAIL)
                )
                self._mark_resolved(job_id, PullOutcome.FAIL)
        else:
            # Hết lượt check này chưa thấy Plus, còn lượt → re-arm (needs attempts==1).
            self._run(
                self.manager.record_plus_check_transition(
                    job_id, "checking", "armed", attempts_now
                )
            )

    # ------------------------------------------------------------------
    # Rule: fail — worker bấm Thất_Bại_Button
    # ------------------------------------------------------------------
    @rule(job_idx=st.integers(min_value=0, max_value=_NUM_ACCOUNTS - 1))
    def fail(self, job_idx: int) -> None:
        job_id = self._job_ids[job_idx]
        record = self._pending_assigned(job_id)
        if record is None:
            return

        # Đánh dấu failed_marked (best-effort theo state hiện tại) rồi resolve FAIL.
        state = record.plus_check_state.value
        if state in ("armed", "checking"):
            self._run(
                self.manager.record_plus_check_transition(
                    job_id, state, "failed_marked", record.plus_check_attempts
                )
            )
        self._run(self.manager.resolve_pull_outcome(job_id, PullOutcome.FAIL))
        self._mark_resolved(job_id, PullOutcome.FAIL)

    # ------------------------------------------------------------------
    # Rule: simulate_error — lỗi kỹ thuật → resolve_pull_error → FAIL
    # ------------------------------------------------------------------
    @rule(job_idx=st.integers(min_value=0, max_value=_NUM_ACCOUNTS - 1))
    def simulate_error(self, job_idx: int) -> None:
        job_id = self._job_ids[job_idx]
        record = self._pending_assigned(job_id)
        if record is None:
            return

        self._run(self.manager.resolve_pull_error(job_id, "some_error"))
        # resolve_pull_error swallow JobAlreadyResolvedError; ở đây job chắc
        # chắn còn PENDING (đã check ở _pending_assigned) nên resolve thành công.
        self._mark_resolved(job_id, PullOutcome.FAIL)

    # ------------------------------------------------------------------
    # Rule: switch_mode — Mode_Switch_Guard
    # ------------------------------------------------------------------
    @rule(to_pull=st.booleans(), force=st.booleans())
    def switch_mode(self, to_pull: bool, force: bool) -> None:
        new_mode = "pull" if to_pull else "push"

        # Chụp danh sách job sẽ bị force-fail TRƯỚC khi gọi (để cập nhật shadow).
        pending_before = [
            r.job.job_id
            for r in self.manager.list_jobs()
            if r.pull_assignment_state == PullAssignmentState.ASSIGNED
            and r.pull_outcome == PullOutcome.PENDING
        ]

        try:
            self._run(self.manager.set_operating_mode(new_mode, force=force))
        except ModeSwitchBlockedError:
            # force=False + còn job dở dang → chặn hợp lệ, không đổi state.
            assert not force
            assert pending_before
            return
        except (JobNotFoundError, JobAlreadyResolvedError):  # pragma: no cover
            # Data-integrity: chỉ xảy ra nếu resolve giữa vòng lặp lỗi — trong
            # mô hình tuần tự này không thể xảy ra; nếu có, để test fail rõ.
            raise

        # Tới đây: mode đã đổi thành công. Nếu force → mọi job pending trước
        # đó đã bị chốt FAIL.
        if force:
            for job_id in pending_before:
                self._mark_resolved(job_id, PullOutcome.FAIL)

    # ------------------------------------------------------------------
    # Invariants — chạy sau MỖI rule (Requirement 22.1–22.8)
    # ------------------------------------------------------------------
    @invariant()
    def inv_22_1_worker_assignment_stable(self) -> None:
        """22.1: job đã gán không bao giờ đổi worker."""
        for job_id, worker in self._first_worker.items():
            record = self.manager.get_job(job_id)
            if record is None:
                continue
            assert record.pull_assigned_telegram_user_id == worker, (
                f"22.1 vi phạm: job {job_id} đổi worker "
                f"{worker} -> {record.pull_assigned_telegram_user_id}"
            )

    @invariant()
    def inv_22_2_worker_within_limit(self) -> None:
        """22.2: không worker nào vượt limit concurrent hiện hành."""
        for worker in _WORKERS:
            active = self.manager.get_worker_active_pull_count(worker)
            assert active <= _LIMIT_PER_WORKER, (
                f"22.2 vi phạm: worker {worker} có {active} job active "
                f"> limit {_LIMIT_PER_WORKER}"
            )

    @invariant()
    def inv_22_3_worker_totals_monotonic(self) -> None:
        """22.3: success_count + fail_count mỗi worker đơn điệu không giảm."""
        totals: dict[str, int] = {w: 0 for w in _WORKERS}
        for worker, success_delta, fail_delta, _u, _f in self.repo.calls:
            if worker in totals:
                totals[worker] += success_delta + fail_delta
        for worker in _WORKERS:
            assert totals[worker] >= self._prev_worker_totals[worker], (
                f"22.3 vi phạm: worker {worker} tổng công GIẢM "
                f"{self._prev_worker_totals[worker]} -> {totals[worker]}"
            )
            self._prev_worker_totals[worker] = totals[worker]

    @invariant()
    def inv_22_4_and_22_7_outcome_absorbing(self) -> None:
        """22.4 + 22.7: pull_outcome đã chốt là terminal và bất biến.

        (22.4 double-count được chặn thêm ở ``_mark_resolved``.)"""
        for job_id, outcome in self._resolved.items():
            record = self.manager.get_job(job_id)
            if record is None:
                continue
            assert record.pull_outcome == outcome, (
                f"22.7 vi phạm: pull_outcome của job {job_id} đổi khỏi giá trị "
                f"terminal {outcome} -> {record.pull_outcome}"
            )
            assert outcome in (PullOutcome.SUCCESS, PullOutcome.FAIL)

    @invariant()
    def inv_22_5_no_account_double_assigned(self) -> None:
        """22.5: mọi job ASSIGNED phải nằm trong shadow (gán đúng 1 lần)."""
        for record in self.manager.list_jobs():
            if record.pull_assignment_state == PullAssignmentState.ASSIGNED:
                assert record.job.job_id in self._first_worker, (
                    f"22.5 vi phạm: job {record.job.job_id} ASSIGNED nhưng "
                    "không qua đúng 1 lần claim theo dõi được"
                )

    @invariant()
    def inv_22_8_plus_check_attempts_bounded(self) -> None:
        """22.8: plus_check_attempts <= 2 với mọi record."""
        for record in self.manager.list_jobs():
            assert record.plus_check_attempts <= 2, (
                f"22.8 vi phạm: job {record.job.job_id} có "
                f"plus_check_attempts={record.plus_check_attempts} > 2"
            )

    # ------------------------------------------------------------------
    # Teardown — dọn task nền + đóng loop để tránh leak giữa các example.
    # ------------------------------------------------------------------
    def teardown(self) -> None:
        async def _cleanup() -> None:
            scheduler_task = self.manager._scheduler_task  # noqa: SLF001
            cleanup_task = self.manager._cleanup_task  # noqa: SLF001
            for task in (scheduler_task, cleanup_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            for record in self.manager.list_jobs():
                handler_task = record.handler_task
                if handler_task is not None and not handler_task.done():
                    handler_task.cancel()
                    try:
                        await handler_task
                    except (asyncio.CancelledError, Exception):
                        pass

        try:
            self._run(_cleanup())
        finally:
            self._loop.close()
            asyncio.set_event_loop(None)


def test_pull_mode_invariants_22_1_to_22_8() -> None:
    """Chạy state machine dưới profile fast (deadline=None vì có task nền)."""
    run_state_machine_as_test(
        PullModeInvariantsMachine,
        settings=hyp_settings(deadline=None),
    )
