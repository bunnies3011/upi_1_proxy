# Báo cáo migrate `httpx` → `curl_cffi` (bypass Cloudflare 403)

**Ngày:** 2026-07-06
**Branch:** main
**Scope:** backend (Python)

---

## 1. Bối cảnh

Cloudflare trả **403** cho request đi từ `httpx.AsyncClient` — không phải vì header (đã giả Chrome 141 macOS + Sec-CH-UA hints ở `flow.py:135-169`), mà vì **TLS/JA3 + HTTP/2 fingerprint** của Python vẫn khác Chrome thật.

**Bằng chứng trong repo trước migrate:**
- `chatgpt_login.py:25-27`: "Nếu Cloudflare strict với fingerprint sẽ fail — caller phải đổi proxy hoặc thêm `curl_cffi` dependency."
- `stripe_token.py:3-5`: "refactor `curl_cffi.Session` → `httpx.AsyncClient`" (đây là bước ĐẢO NGƯỢC).
- `app/__init__.py`: env-var `IDEAL_QR_TOOL_DISABLE_TRUSTSTORE=1` thêm chuyên biệt để debug Cloudflare 403.

**Quyết định người dùng (via AskUserQuestion):**
1. Migrate CẢ tests (14 file).
2. Gỡ hẳn `httpx` khỏi `pyproject.toml`.
3. Profile `impersonate="chrome131"`.

**Kết quả kỳ vọng:** mọi request → `chatgpt.com` / `api.stripe.com` / `pay.ideal.nl` / `sentinel.openai.com` / `js.stripe.com` / `api.telegram.org` / proxy probe đều đi qua `curl_cffi.requests.AsyncSession` với JA3/TLS fingerprint Chrome 131 → không còn 403.

---

## 2. Kết quả pytest

| | Failed | Passed | Total |
|---|---|---|---|
| **Pre-migration** | 31 | 469 | 500 |
| **Post-migration** | **21** | **479** | 500 |
| **Delta** | **-10** | **+10** | 0 |

- **10 tests được fix** nhờ migration (tests cũ đã broken vì phụ thuộc quirk cụ thể của httpx transport).
- **0 regression** — mọi test đang pass tiếp tục pass.
- 21 failures còn lại đều là **pre-existing** (đã tồn tại từ trước, không thuộc scope migrate).

**Danh sách 21 failures pre-existing:**
- `test_job_manager*` (8 tests) — `_FakeProxyPool.acquire()` missing `wait_for_release=` kwarg.
- `test_hide_job_preserves_backend` (2) — pre-existing bug trong test fixtures.
- `test_log_entries_*` (4) — logger signature drift giữa test và prod.
- `test_settings_whitelist_full` (1) — whitelist count drift.
- `test_sse` (1) — SSE payload thêm field `ts` chưa update trong test.
- `test_startup_smoke` (1) — startup validation contract drift.
- `test_hide_job_preserves_backend_data`, `test_job_manager_concurrency_property`, `test_job_manager_stop_property` (3) — cùng root cause `_FakeProxyPool`.
- `test_bounded_retry_logs_request_attempt_status_for_each_step` (1) — logger positional-args contract drift.

---

## 3. Thay đổi production code

### 3.1 Dependencies (`backend/pyproject.toml`)

**Removed:**
- `httpx==0.28.1`
- `respx==0.22.0` (dev)
- `truststore==0.10.1`

**Added:**
- `curl_cffi==0.15.0`

**Bumped version:** `0.1.0` → `0.2.0`

### 3.2 Facade mới (`backend/app/core/http_client.py`)

Điểm tập trung DUY NHẤT cho việc tạo async HTTP client:

```python
from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests import exceptions as _cffi_exc

DEFAULT_IMPERSONATE = "chrome131"

# Aliases giữ semantic API của httpx cho code cũ
TimeoutException = _cffi_exc.Timeout
NetworkError = _cffi_exc.ConnectionError
TransportError = _cffi_exc.RequestException
HTTPError = _cffi_exc.RequestException
DecodingError = _cffi_exc.ContentDecodingError

def create_async_client(*, proxy=None, timeout=30.0, headers=None,
                       cookies=None, allow_redirects=True, verify=True,
                       impersonate="chrome131", **extra) -> AsyncSession: ...

def classify_error(exc) -> str:
    """Trả label: 'timeout'|'proxy_auth'|'dns'|'ssl'|'connect'|'protocol'|'unknown'."""

def is_timeout(exc) -> bool: ...
def is_network_error(exc) -> bool: ...
```

### 3.3 Files thay đổi

**Production (12 files):**
| File | Loại thay đổi |
|---|---|
| `pyproject.toml` | Dep swap + version bump |
| `app/__init__.py` | Xoá truststore inject |
| `app/core/http_client.py` | **MỚI** — facade |
| `app/core/proxy_health.py` | Swap client + rewrite `NETWORK_ERROR_MARKERS` + `_classify_probe_exc` → dùng `classify_error()` |
| `app/payments/ideal/flow.py` | 3 site tạo AsyncClient; xoá `_DEFAULT_HTTP_HEADERS` Sec-CH-UA (impersonate lo); `aclose()` → `close()` |
| `app/payments/ideal/stripe_client.py` | Nhiều exception catch; inline pm-redirects client dùng facade; `content=<bytes>` → `data=<bytes>`; `is_success` → `200 <= status < 300` |
| `app/payments/ideal/chatgpt_login.py` | 9-step OAuth; `follow_redirects=` → `allow_redirects=`; cookie jar iteration verified compatible |
| `app/payments/ideal/chatgpt_client.py` | 5 site cookie `.set()`; nhiều exception catch; `is_success` replace |
| `app/payments/ideal/transaction_client.py` | Swap types + exception |
| `app/payments/ideal/stripe_token.py` | Swap types (nguồn gốc là curl_cffi) |
| `app/payments/ideal/sentinel.py` | Swap types + `content=` → `data=` |
| `app/notifiers/telegram/client.py` | `httpx.AsyncClient(timeout=)` → `http.create_async_client(timeout=)`; `.aclose()` → `.close()` |

### 3.4 Điểm sửa thủ công quan trọng

**a. `flow.py` — `_DEFAULT_HTTP_HEADERS`.**
Xoá 15+ Sec-CH-UA hints; giữ lại `Accept-Language`. `impersonate="chrome131"` tự set User-Agent, Sec-CH-UA-*, Sec-Fetch-*, Accept, Accept-Encoding (gồm Brotli).

**b. `stripe_client.py:245-257` — `_encode_form_urlencoded`.**
Giữ workaround (curl_cffi cũng có edge case với `data=<list-of-tuples>` multipart auto-detect). Cập nhật comment: dùng `data=<bytes>` bypass smart-dispatch.

**c. `stripe_client.py:1720` — Inline pm-redirects client.**
Chuyển từ inline `_httpx.AsyncClient(...)` → `http.create_async_client(...)`. Giữ scope tránh leak `oai-*` headers từ chatgpt_client.

**d. `chatgpt_login.py` — Cookie jar API.**
Verified: `curl_cffi.requests.Cookies.jar` là `http.cookiejar.CookieJar` — iteration + `.name/.value/.domain` attributes identical với httpx. Không cần rewrite.

**e. `proxy_health.py` — `NETWORK_ERROR_MARKERS`.**
Reset list; thêm markers curl_cffi format (`"Failed to perform"`, `"(28) Operation timed out"`, `"(6) Could not resolve host"`, ...). `_classify_probe_exc()` dùng `http.classify_error()` (type-based) làm nguồn chính, fallback string-match.

**f. `stripe_client.py`, `chatgpt_client.py` — Response attribute.**
`response.is_success` (httpx-specific) → `200 <= response.status_code < 300` (7 sites).

**g. `notifiers/telegram/client.py` — Session close.**
`httpx.AsyncClient.aclose()` → `curl_cffi.AsyncSession.close()` (curl_cffi không có `.aclose()`).

---

## 4. Thay đổi tests

### 4.1 Test infrastructure mới

**`tests/support/fake_http.py`** — thay thế `respx` + `httpx.MockTransport`:

```python
class FakeAsyncSession:
    """Fake curl_cffi.requests.AsyncSession cho test.

    Cách 1 — inject vào constructor: StripeClient(http_client=session, ...)
    Cách 2 — monkey-patch factory:
        monkeypatch.setattr(
            "app.core.http_client.create_async_client",
            lambda **kw: session,
        )
    """
    def route(self, method, url, responses, *, regex=False) -> Route: ...
    async def get/post/put/delete/request(...) -> FakeResponse: ...

class FakeResponse:
    """Subset của curl_cffi.Response: status_code, text, content, headers,
    cookies, url, history, json(), raise_for_status()."""
```

**`tests/support/ideal_routes.py`** — shared 12-step iDEAL happy-path route helper:

```python
def register_ideal_happy_path(session, *, checkout_session_id,
                              publishable_key, elements_session_id, ...):
    """Đăng ký 10 route cho 12-step iDEAL:
    - POST chatgpt.com/backend-api/payments/checkout
    - POST api.stripe.com/v1/payment_pages/{cs}/init
    - GET  api.stripe.com/v1/elements/sessions
    - POST api.stripe.com/v1/payment_pages/{cs}  (update_billing)
    - POST api.stripe.com/v1/payment_pages/{cs}/confirm
    - POST chatgpt.com/backend-api/payments/checkout/snapshot
    - POST chatgpt.com/backend-api/payments/checkout/approve
    - GET  api.stripe.com/v1/payment_pages/{cs}  (refresh_poll)
    - GET  {redirect_to_url}  (follow_redirect)
    - POST pay.ideal.nl/api/v1/transactions/{tx}/initiate
    - GET  pay.ideal.nl/transactions/{tx}  (warm cookie)
    - GET  js.stripe.com/v3/  (raise HTTPError → cache fallback)
    """
```

### 4.2 Test files migrated (17 files)

**Nhóm 1 — respx tests (4 file):**
- `tests/unit/test_no_stripe_consumers_lookup.py`
- `tests/unit/test_no_request_after_qr_ready.py`
- `tests/integration/test_ideal_flow_full.py`
- `tests/integration/test_ideal_flow_end_to_end.py`

**Nhóm 2 — `httpx.MockTransport` tests (10 file):**
- `tests/unit/test_no_stripe_lookup_call.py`
- `tests/unit/test_log_entries_complete_fields.py`
- `tests/unit/test_fail_fast_stripe_3_tokens.py`
- `tests/property/test_stripe_client_retry_property.py`
- `tests/property/test_stripe_refresh_poll_property.py`
- `tests/property/test_stripe_follow_redirect_property.py`
- `tests/property/test_stripe_confirm_invalid_response_property.py`
- `tests/property/test_stripe_confirm_body_property.py`
- `tests/property/test_stripe_confirm_address_reject_property.py`
- `tests/property/test_approve_call_count_property.py`

**Nhóm 3 — classifier property tests (3 file):**
- `tests/property/test_login_error_classification_property.py`
- `tests/property/test_processor_entity_property.py`
- `tests/property/test_transaction_client_sig_property.py`
- `tests/property/test_transaction_client_validation_property.py`

### 4.3 Migration pattern (mechanical)

```python
# Import
- import httpx
- import respx
+ from tests.support.fake_http import FakeAsyncSession, FakeResponse
+ from app.core import http_client as http  # nếu cần exception classes

# Handler signature
- def _handler(request: httpx.Request) -> httpx.Response:
+ def _handler(call):  # noqa: ANN001

# Response construction
- httpx.Response(200, json={...})
+ FakeResponse(200, json_body={...})

# Exception in handler
- raise httpx.TimeoutException("msg")
+ return http.TimeoutException("msg")  # FakeAsyncSession auto-raises

# Client construction
- transport = httpx.MockTransport(handler)
- async with httpx.AsyncClient(transport=transport) as http_client:
-     client = SomeClient(http_client=http_client, ...)
+ fake_session = FakeAsyncSession(default_handler=handler)
+ client = SomeClient(http_client=fake_session, ...)
```

---

## 5. Rủi ro & mitigation

**a. curl_cffi wheel không có cho platform target.**
- Có prebuilt wheel: macOS (arm64/x86_64), Linux glibc, Windows.
- Nếu build offline không có wheel → cần internet.
- Mitigation: verified `pip install curl_cffi==0.15.0` OK trên darwin arm64.

**b. `impersonate="chrome131"` không đủ để bypass.**
Cloudflare có thể strict hơn (CH-UA client hints, cookie `__cf_bm` warm-up).
- Mitigation: giữ nguyên `_prime()` warm-up step ở `chatgpt_login.py:192-226`.
- Fallback options: `chrome124`, `chrome136`, `chrome142` (đổi 1 dòng ở `http_client.DEFAULT_IMPERSONATE`).
- Escape hatch cuối cùng: thêm proxy residential.

**c. Cookie API sai lệch nhỏ.**
- Verified: `curl_cffi.requests.Cookies.jar` là `http.cookiejar.CookieJar` — iteration + `.name/.value/.domain` identical với httpx.
- `.get(name)` raise `CookieConflict` khi trùng — cùng semantic httpx.

**d. Follow-redirect behavior thay đổi.**
- `stripe_client.follow_redirect()` giờ dùng `allow_redirects=True` + đọc `response.url` (thay vì manual 302 + Location header).
- Test `test_stripe_follow_redirect_property.py` updated: chỉ cover nhánh non-2xx (status ∈ [300, 599]) — nhánh 2xx nay đi qua parse url path.

---

## 6. Verification

### 6.1 Static grep

```bash
grep -rn "^import httpx\|^from httpx\|^import respx\|^from respx" backend/app backend/tests
```
Kết quả: **0 hit** (files trong `backend/test/` là debug scripts deprecated — không migrate theo plan).

### 6.2 Import chain

```bash
python -c "
import app
from app.core import http_client
from app.core import proxy_health
from app.payments.ideal import (
    sentinel, transaction_client, stripe_token,
    chatgpt_client, chatgpt_login, stripe_client, flow
)
from app.notifiers.telegram import client as telegram_client
"
```
Kết quả: **All imports OK**.

### 6.3 Pytest

Đã chạy `pytest tests/` → **479 passed, 21 failed** (tất cả failed đều pre-existing, confirmed via `git stash`).

### 6.4 Bước verify runtime khuyên chạy tiếp

**a. Manual smoke ChatGPT login.**
```bash
cd backend
./.venv/bin/python -m app.cli.main
# Với 1 tài khoản test — quan sát log request đầu tiên GET https://chatgpt.com/auth/login
# Kỳ vọng: 200 (không 403).
```

**b. Nếu vẫn 403 → fallback thử profile khác:**
```python
# backend/app/core/http_client.py
DEFAULT_IMPERSONATE: Final[str] = "chrome124"  # hoặc chrome136 / chrome142
```

**c. Full iDEAL end-to-end.**
Chạy full 12-step flow với real account + real proxy; xác nhận QR code render đúng.

**d. Regression Stripe/Telegram.**
Chạy 1 job đầy đủ; xác nhận Telegram notification vẫn gửi được (curl_cffi multipart hoạt động).

**e. Proxy health probe.**
```python
from app.core.proxy_health import probe_proxy
await probe_proxy("http://user:pass@host:port",
                  endpoint="https://api64.ipify.org", timeout=6)
# Với proxy sống → (True, "ok")
# Với proxy 407 → (False, "auth")
# Với DNS fail → (False, "auth")
```

---

## 7. Commit đề xuất

```
refactor(http): swap httpx → curl_cffi to bypass Cloudflare TLS fingerprint

- Migrate all backend HTTP calls (chatgpt.com, api.stripe.com, pay.ideal.nl,
  telegram, proxy_health) from httpx.AsyncClient to curl_cffi.AsyncSession
  with impersonate="chrome131" (Chrome TLS/JA3/H2 fingerprint).
- Add app/core/http_client.py facade with exception aliases + classify_error().
- Remove truststore (curl_cffi uses BoringSSL, not Python ssl module).
- Replace respx tests with FakeAsyncSession-based test infrastructure
  (tests/support/fake_http.py, tests/support/ideal_routes.py).
- Test suite: 479 passed / 21 failed (down from 469 / 31 — 10 tests fixed,
  0 regression; remaining failures are pre-existing unrelated bugs).
```

---

## 8. Files reference

**Plan file:** `/Users/linhtran/.claude/plans/snug-imagining-sifakis.md`

**Facade:** `backend/app/core/http_client.py`

**Test support:**
- `backend/tests/support/fake_http.py`
- `backend/tests/support/ideal_routes.py`

**Pre-existing failing tests (không thuộc scope migration — cần fix riêng):**
- `backend/tests/unit/test_job_manager.py` (8 tests)
- `backend/tests/unit/test_hide_job_preserves_backend.py` (2)
- `backend/tests/unit/test_log_entries_required_fields.py` (3)
- `backend/tests/unit/test_log_entries_complete_fields.py::test_bounded_retry_logs_request_attempt_status_for_each_step`
- `backend/tests/unit/test_settings_whitelist_full.py`
- `backend/tests/unit/test_sse.py`
- `backend/tests/unit/test_startup_smoke.py::test_startup_non_loopback_no_token_fails`
- `backend/tests/property/test_job_manager_concurrency_property.py`
- `backend/tests/property/test_job_manager_stop_property.py`
