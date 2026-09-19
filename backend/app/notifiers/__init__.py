"""Namespace `notifiers/` — các module gửi thông báo out-of-band.

Cùng cấp với `payments/` — separation of concerns:

- `payments/*`: implement `PaymentFlowHandler` (nghiệp vụ chính, chạy flow
  cho từng job và trả `JobResult`).
- `notifiers/*`: react tới sự kiện job đã hoàn thành (VD Telegram bot gửi
  QR đến chat), KHÔNG can thiệp flow chính.

Boundary tương tự `payments/`:

- `core/` KHÔNG import bất kỳ gì từ `notifiers/*`.
- `notifiers/*` được phép import `app.core.*` (settings, job_manager) để
  đăng ký namespace/hook, nhưng KHÔNG import bất kỳ `payments/*` cụ thể
  nào (giữ generic — cùng notifier hoạt động với mọi payment method).
- `bootstrap.py` là điểm duy nhất wire notifier vào `JobManager` qua
  `register_terminal_hook`.
"""
