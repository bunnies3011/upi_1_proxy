# GCash Direct Flow

File này ghi lại logic GCash hiện tại trong repo `UPI-QR-root-fix`, dựa trên các module:

- `backend/app/payments/gcash_direct/flow.py`
- `backend/app/payments/gcash_direct/post_login_flow.py`
- `backend/app/payments/gcash_direct/browser_qr.py`
- `backend/app/payments/gcash_direct/__init__.py`
- lớp login shell kế thừa từ `backend/app/payments/upi_direct/flow.py`

GCash Direct là flow native của OpenAI cho Philippines/PHP. Điểm quan trọng nhất: checkout id hợp lệ là `oaics_*`, không phải Stripe `cs_*`. Không được đem `oaics_*` đi gọi Stripe payment page/init.

## Mục Tiêu

Input là một dòng account ChatGPT:

```text
email|password|totp_secret
```

Output mong muốn:

- `payment_link_ready`: link Adyen redirect từ OpenAI native checkout.
- `payment_link_resolved`: link cuối sau khi mở bằng browser session account, thường là trang `m.gcash.com/...`.
- QR PNG hợp lệ lấy từ DOM của trang GCash, lưu thành artifact.
- Job lên `QR_READY` để UI hiện ở tab `QR ready`, có copy link và mở QR.
- Sau khi người dùng quét QR bằng app GCash, browser đang giữ session nhận callback về ChatGPT; sau đó check plan mới có thể lên `PLUS`.

## Khác Với UPI Direct

UPI Direct đi theo Stripe/Payment Element và có các bước Stripe như `stripe_init`, `stripe_elements`, token config, confirm UPI.

GCash Direct không chạy Stripe init. GCash đi qua OpenAI native checkout:

```text
ChatGPT checkout -> GCash availability check -> ChatGPT native confirm -> custom payment method start -> Adyen redirect -> browser session -> GCash QR
```

Nếu log xuất hiện lỗi kiểu:

```text
No such payment_page: 'oaics_...'
```

thì nghĩa là flow đã sai hướng, vì `oaics_*` không phải Stripe payment page.

## Flow Tổng Quan

Luồng chuẩn:

```text
1. Nhận account line
2. Login hoặc lấy session cache ChatGPT
3. Tạo checkout native PH/PHP qua ChatGPT
4. Kiểm tra account có GCash custom payment method không
5. Confirm native checkout với custom payment method id
6. Start native custom payment method flow
7. Lấy Adyen redirect link
8. Mở link bằng browser có session của đúng account ChatGPT
9. Browser redirect sang m.gcash.com
10. Lấy QR PNG từ selector #qrcode img[src^="data:image/png;base64,"]
11. Lưu QR artifact, trả QR_READY + payment_link
12. Giữ browser sống để nhận callback sau khi quét QR
13. Check plan sau scan để xác nhận Plus
```

## Entry Point

`GcashDirectFlowHandler` trong `flow.py` kế thừa `UpiDirectFlowHandler`.

Nó dùng lại phần login/session/proxy shell của UPI Direct, nhưng thay post-login runner bằng:

```python
app.payments.gcash_direct.post_login_flow.run_post_login
```

Cấu hình handler:

```text
payment_method = gcash_direct
max_concurrent_key = gcash_direct.max_concurrent
run_timeout_key = gcash_direct.run_timeout_seconds
require_promo_key = gcash_direct.require_promo
proxy_checkout_key = gcash_direct.proxy_checkout
proxy_promotion_key = gcash_direct.proxy_promotion
```

## Bước 1: Parse Account

Account được parse bằng parser ChatGPT chung trong `_chatgpt.models`.

Nếu line sai format, handler trả:

```text
status=ERROR
error_code=invalid_account_line
```

Format chuẩn:

```text
email|password|totp_secret
```

## Bước 2: Login / Session Cache

`UpiDirectFlowHandler.run()` xử lý:

- tạo logger có redaction secrets
- parse account
- resolve proxy pool
- tạo HTTP client login
- gọi `resolve_session(...)`

Nếu session cache còn dùng được, log thường là:

```text
session_cache hit account_key=...
```

Nếu cache stale hoặc invalid, flow login lại bằng email/password/TOTP:

```text
chatgpt_login begin
[login] start
[login] password/verify
[login] MFA verify (TOTP)
[login] GET /api/auth/session
chatgpt_login ok
```

Session quan trọng gồm:

- `access_token`
- cookies ChatGPT/OpenAI

GCash QR browser về sau phải dùng đúng cookies của session này. Đây là điểm quyết định: QR được render trong browser phải thuộc đúng account vừa tạo checkout.

## Bước 3: Chọn Proxy Cho GCash

Trong `run_post_login`, GCash tự chọn proxy từ 2 pool riêng:

```text
gcash_direct.proxy_checkout
gcash_direct.proxy_promotion
```

Logic chọn:

```python
pick_a = pick_and_materialize(pools.checkout_lines)
pick_b = pick_and_materialize(pools.promotion_lines)
pick_main = pick_a or pick_b
pick_promo = pick_b or pick_main
```

Log:

```text
gcash proxy main=checkout_A promo=promotion_B
gcash proxy main=promotion_B_fallback promo=promotion_B
gcash proxy main=direct
```

Ưu tiên hiện tại:

1. Pool A handles login/bare checkout/taxes/confirm/start/settle.
2. Pool B only handles `/backend-api/payments/checkout/update` promo.
3. If Pool A is empty, the tool can fallback to B/direct for single-proxy mode.
4. For GCash PH/PHP, Pool A must be PH. A=JP/KR will fail with `Billing country must match request country`.

HTTP client post-login dùng:

```python
proxy=pick_main.materialized_url if pick_main is not None else None
```

Browser QR mặc định không dùng proxy. Browser chỉ dùng proxy nếu setting:

```text
gcash_direct.browser_use_proxy = true
```

Nếu setting này false, browser mở link bằng direct network, dù checkout/native API trước đó dùng proxy.

## Bước 4: Tạo Native Checkout PH/PHP

Function:

```python
_create_gcash_checkout(...)
```

Endpoint:

```text
POST https://chatgpt.com/backend-api/payments/checkout
```

Body chính:

```json
{
  "entry_point": "all_plans_pricing_modal",
  "plan_name": "chatgptplusplan",
  "billing_details": {
    "country": "PH",
    "currency": "PHP"
  },
  "checkout_ui_mode": "custom",
  "promo_campaign": {
    "promo_campaign_id": "plus-1-month-free",
    "is_coupon_from_query_param": false
  }
}
```

Headers quan trọng:

```text
Authorization: Bearer <access_token>
Origin: https://chatgpt.com
Referer: https://chatgpt.com/?promo_campaign=plus-1-month-free
OAI-Language: en-PH
x-openai-target-path: /backend-api/payments/checkout
x-openai-target-route: /backend-api/payments/checkout
```

Response phải có:

```text
checkout_session_id = oaics_...
```

Log chuẩn:

```text
chatgpt_checkout ok checkout_session_id=oaics_... provider=open_ai currency=PHP
```

Nếu checkout id không bắt đầu bằng `oaics_`, code chỉ warning:

```text
gcash checkout returned non-native session id prefix=...
```

Nhưng về mặt flow GCash chuẩn, session phải là `oaics_*`.

## Bước 5: Check GCash Available

Sau checkout, code tìm GCash qua field:

```python
custom_payment_methods
```

Function:

```python
_extract_custom_payment_method_id(payload)
```

Điều kiện:

```text
custom_payment_methods[*].id startswith cpmt_
```

Log có GCash:

```text
gcash_check available=true custom_payment_method_id=cpmt_...
```

Log không có GCash:

```text
gcash_check available=false payment_method_types=...
```

Nếu không có `cpmt_...`, flow fail:

```text
error_code=gcash_not_offered
step=checkout
message=GCash not offered
```

Điểm đã xác nhận khi test: GCash có vẻ phụ thuộc account/offer/checkout response. Không phải cứ country PH/PHP là chắc chắn có GCash.

## Bước 6: Amount / Promo Gate

Code đọc amount từ nhiều path:

```text
checkout_state.total.total.minorUnitsAmount
total.total.minorUnitsAmount
total_summary.due
amount_total
```

Nếu setting:

```text
gcash_direct.require_promo = true
```

và amount > 0, flow fail bằng `NoFreeOfferError`.

Mục tiêu hiện tại là claim free promo, nên amount cần là 0 hoặc không bị xác định là phí > 0.

## Bước 7: Native Confirm

Function:

```python
_gcash_confirm_native(...)
```

Endpoint:

```text
POST https://chatgpt.com/backend-api/payments/checkout/confirm
```

Body:

```json
{
  "checkout_session_id": "oaics_...",
  "selected_payment_method_type": "cpmt_..."
}
```

Referer:

```text
https://chatgpt.com/checkout/openai_llc/oaics_...
```

Log chuẩn:

```text
gcash_confirm ok checkout_session_id=oaics_... status=success keys=status,confirm_return_url
```

Nếu confirm payload có status:

```text
blocked
rejected
failed
declined
```

thì flow fail:

```text
error_code=gcash_native_failed
step=gcash_confirm
```

Quan trọng: flow GCash hiện tại không có bước `chatgpt_approve` giữa confirm và start. Không được tự thêm approve kiểu UPI/Kakao ở đoạn này nếu không có bằng chứng từ flow native.

## Bước 8: Start Custom Payment Method

Function:

```python
_gcash_start_native(...)
```

Endpoint:

```text
POST https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start
```

Body:

```json
{
  "checkout_session_id": "oaics_...",
  "custom_payment_method_type_id": "cpmt_..."
}
```

Response chứa redirect URL trong `next_action.url` hoặc một field URL nested.

Function tìm link:

```python
_find_gcash_redirect_url(data)
```

Nó ưu tiên:

```text
next_action.url
```

sau đó walk toàn payload để tìm URL có marker:

```text
adyen
gcash
checkoutshopper
redirect
```

Log chuẩn:

```text
payment_link_ready payment_link=https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=...
```

Nếu không tìm thấy link:

```text
error_code=gcash_redirect_missing
step=payment_link
```

## Bước 9: Vì Sao Không Được Tự Render QR Từ Link

Adyen redirect link không phải QR cuối để quét bằng GCash.

QR tự render từ URL này có thể scan fail hoặc scan xong không kích hoạt Plus, vì thiếu browser session đúng account và callback context.

Quy tắc:

```text
Không tự chế QR từ payment_link.
Không dùng QR fallback bằng URL backend redirect ngắn cho GCash.
QR hợp lệ phải là QR PNG GCash render ra trong browser session đã login đúng account ChatGPT.
```

## Bước 10: Browser Session Để Lấy QR

Function:

```python
capture_gcash_browser_qr(...)
```

Điều kiện bật:

```text
gcash_direct.browser_qr_enabled = true
```

Browser dùng Playwright Chromium.

Context desktop:

```text
viewport 1360x900
locale en-PH
timezone Asia/Manila
user_agent desktop Edge/Chrome
```

Cookies được inject từ `SessionBundle.cookies`, chỉ lấy các cookie liên quan:

```text
__Secure-next-auth.session-token*
__Host-next-auth.csrf-token*
__Secure-next-auth.csrf-token*
oai-did
oai-sc
cf_clearance
```

Browser mở:

```text
https://chatgpt.com/
```

rồi check session bằng:

```text
fetch('/api/auth/session', { credentials: 'include' })
```

Log:

```text
gcash_browser session_status=ok
gcash_browser session_status=http_403
gcash_browser session_status=no_access_token
```

`ok` là tốt nhất. Nếu không ok, QR vẫn có thể hiện nhưng khả năng callback/Plus sai cao hơn.

Sau đó browser mở Adyen redirect link:

```text
page.goto(payment_link)
```

Rồi chờ URL không còn host:

```text
checkoutshopper-live.adyen.com
```

Thường final URL là:

```text
https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?...
```

Log:

```text
gcash_browser opened url=https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?...
```

Sau khi có final URL, flow set:

```text
payment_link_resolved payment_link=https://m.gcash.com/...
```

và `JobResult.payment_link` sẽ là link final này nếu resolve được.

## Bước 11: Selector Lấy QR Đúng

QR được lấy trực tiếp từ DOM bằng selector:

```css
#qrcode img[src^="data:image/png;base64,"]
.qr-container #qrcode img[src^="data:image/png;base64,"]
.qr-section #qrcode img[src^="data:image/png;base64,"]
.instructions-container #qrcode img[src^="data:image/png;base64,"]
```

Điều kiện ảnh:

- element visible
- width >= 160
- height >= 160
- tỷ lệ gần vuông, ratio trong khoảng `0.72..1.28`

Sau đó:

```text
data:image/png;base64,...
```

được decode base64, validate PNG header, re-encode/validate bằng helper network safety, rồi ghi artifact:

```text
runtime/qr/<job_id>.png
```

Log chuẩn:

```text
gcash_browser_qr ok artifact=... elapsed=...
```

Nếu không lấy được QR trong timeout:

```text
gcash_browser_qr failed: qr_data_uri_not_found url=...
```

Điểm quan trọng: QR fail không làm mất link. Flow vẫn có thể trả `QR_READY` với `payment_link`, nhưng `artifact_path=None`.

## Bước 12: Giữ Browser Sống

Sau khi lấy QR, browser không đóng ngay nếu:

```text
gcash_direct.browser_hold_seconds > 0
gcash_direct.browser_hold_max_active > 0
```

Default hiện tại:

```text
browser_hold_seconds = 300
browser_hold_max_active = 5
```

Mục đích: giữ tab checkout có session account để khi điện thoại scan và authorize trong app GCash, callback từ Adyen/GCash quay về ChatGPT được.

Log hold:

```text
gcash_browser_hold begin seconds=300
gcash_browser_hold url=https://m.gcash.com/...
gcash_browser_hold url=https://chatgpt.com/checkout/verify?...
gcash_browser_hold url=https://chatgpt.com/payments/success?...
gcash_browser_hold completion_seen closing
gcash_browser_hold closed
```

Completion URL được nhận diện khi host là `chatgpt.com` và một trong các điều kiện:

```text
path == /payments/success
query contains refresh_account=true
fragment contains plus_onboarding
```

Nếu thấy completion URL liên tục ít nhất 20s, browser tự đóng sớm.

Nếu số browser hold đang chạy vượt `browser_hold_max_active`, code cancel hold cũ nhất:

```text
gcash_browser_hold limit cancel_oldest active=... max=...
```

## Bước 13: JobResult / UI State

Khi có link, flow trả:

```python
JobResult(
    status=JobStatus.QR_READY,
    artifact_path=artifact_path,
    payment_link=final_payment_link,
    plan="free",
    proxy_lease_health=health_box[0],
)
```

Ý nghĩa:

- `QR_READY`: đã tạo được link GCash, và có thể có QR artifact.
- `artifact_path`: có nếu browser lấy được QR PNG.
- `payment_link`: nếu browser resolve được thì là link `m.gcash.com`; nếu không thì là link Adyen redirect ban đầu.
- `plan="free"`: trạng thái ban đầu trước khi người dùng scan/authorize và trước khi check-plan xác nhận Plus.

`QR_READY` không đồng nghĩa đã lên Plus. Nó chỉ nghĩa là QR/link đã sẵn sàng.

Success thật sự cần check plan sau khi scan:

```text
chatgpt_check_plan entitlement ok: email=... plan=plus
```

Khi check plan trả `plus`, UI mới nên coi account là Plus.

## Log Chuẩn Thành Công

Ví dụ log tốt:

```text
proxy_acquired
flow start email=... mode=direct
session_cache hit account_key=...
gcash proxy main=checkout_A promo=promotion_B
chatgpt_checkout ok checkout_session_id=oaics_... provider=open_ai currency=PHP
gcash_check available=true custom_payment_method_id=cpmt_...
gcash_confirm ok checkout_session_id=oaics_... status=success keys=status,confirm_return_url
payment_link_ready payment_link=https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=...
gcash_browser begin timeout=35s proxy=direct
gcash_browser session_status=ok
gcash_browser opened url=https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?...
gcash_browser_qr ok artifact=... elapsed=...
payment_link_resolved payment_link=https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?...
gcash_browser_hold begin seconds=300
```

Sau khi người dùng quét và authorize:

```text
gcash_browser_hold url=https://chatgpt.com/checkout/verify?...
gcash_browser_hold url=https://chatgpt.com/payments/success?...
chatgpt_check_plan entitlement ok: email=... plan=plus
```

## Settings GCash

Namespace:

```text
gcash_direct.*
```

Defaults hiện tại:

| Key | Default | Ý nghĩa |
| --- | --- | --- |
| `gcash_direct.proxy_checkout` | `[]` | Pool A: login, bare checkout, taxes, confirm, start, settle. Dung PH proxy cho GCash PH/PHP |
| `gcash_direct.proxy_promotion` | `[]` | Pool B: only promo `/checkout/update`; blank reuses Pool A |
| `gcash_direct.max_concurrent` | `3` | Số job GCash chạy song song |
| `gcash_direct.run_timeout_seconds` | `300` | Timeout tổng cho job |
| `gcash_direct.stripe_request_timeout_seconds` | `30` | Legacy/shared setting, GCash native hiện không chạy Stripe init |
| `gcash_direct.approve_error_retries` | `1` | Legacy/shared setting, flow GCash hiện không dùng approve step |
| `gcash_direct.require_promo` | `true` | Bắt buộc amount free/promo |
| `gcash_direct.browser_qr_enabled` | `true` | Bật mở browser session để resolve link/lấy QR |
| `gcash_direct.browser_qr_capture_enabled` | `true` | Bật lấy QR PNG từ selector |
| `gcash_direct.browser_qr_timeout_seconds` | `35` | Timeout chờ browser/QR |
| `gcash_direct.browser_hold_seconds` | `300` | Giữ browser sống sau QR |
| `gcash_direct.browser_hold_max_active` | `5` | Số browser hold tối đa |
| `gcash_direct.browser_headless` | `true` | Browser chạy headless |
| `gcash_direct.browser_use_proxy` | `false` | Browser QR có dùng proxy GCash API hay không |

## Các Lỗi Thường Gặp

### 1. No such payment_page: oaics_...

Sai flow. `oaics_*` là OpenAI native checkout id, không phải Stripe payment page.

Không được gọi Stripe init với `oaics_*`.

### 2. gcash_not_offered

Log:

```text
gcash_check available=false payment_method_types=...
```

Account/checkout response không có GCash custom payment method `cpmt_...`.

Không phải reject ở confirm; là chưa có method để chọn.

### 3. Checkout trả currency sai

GCash phải tạo checkout PH/PHP:

```text
billing_country=PH
billing_currency=PHP
currency=PHP
```

Nếu còn INR/KRW hoặc session/link cũ, flow đang sai country/currency hoặc cache/checkout path.

### 4. Confirm blocked/rejected/failed/declined

Nếu `_gcash_confirm_native` trả status nằm trong:

```text
blocked, rejected, failed, declined
```

flow fail ở `gcash_confirm`.

### 5. Start 409 "Checkout session must be confirmed before starting..."

Thường do gọi start khi confirm chưa thực sự success hoặc code chen thêm bước sai giữa confirm/start.

Flow đúng hiện tại:

```text
gcash_confirm ok status=success -> gcash_start_native
```

Không thêm approve.

### 6. QR image scan được nhưng không lên Plus

Nguyên nhân chính đã gặp: QR/link được lấy ngoài browser session đúng account, hoặc browser bị đóng sớm nên callback không quay về ChatGPT.

Flow đúng:

```text
login đúng account -> mở payment_link trong browser có cookies account đó -> lấy QR từ DOM -> giữ browser sống -> scan/authorize -> callback -> check plan
```

### 7. QR code is not valid

Nếu QR được render từ raw URL hoặc redirect short URL thì có thể invalid trong app GCash.

Chỉ dùng QR PNG từ:

```css
#qrcode img[src^="data:image/png;base64,"]
```

### 8. playwright_missing

Browser QR không chạy vì thiếu Playwright hoặc browser runtime.

Log:

```text
gcash_browser_qr failed: playwright_missing
```

Flow vẫn có thể trả link, nhưng không có QR artifact.

### 9. gcash_browser session_status=http_403

Browser không đọc được `/api/auth/session` trong context ChatGPT. Có thể do cookie/cache/proxy/Cloudflare.

QR có thể vẫn render, nhưng xác suất callback/Plus sai cao hơn. Session status tốt nhất là:

```text
gcash_browser session_status=ok
```

## Checklist Khi Debug

Kiểm tra theo thứ tự:

1. Log checkout có `oaics_...` không.
2. Currency checkout là `PHP` không.
3. Có log `gcash_check available=true custom_payment_method_id=cpmt_...` không.
4. Confirm có `status=success` không.
5. Có `payment_link_ready` không.
6. Browser có `session_status=ok` không.
7. Browser final URL có phải `m.gcash.com/...` không.
8. QR artifact có log `gcash_browser_qr ok artifact=...` không.
9. Browser hold có đang sống khi người dùng scan không.
10. Sau scan có URL `chatgpt.com/payments/success` hoặc `refresh_account=true` không.
11. Check plan cuối cùng có `plan=plus` không.

## Quy Tắc Không Được Phá

- Không gửi `oaics_*` vào Stripe.
- Không tự render QR từ raw Adyen URL.
- Không fallback QR sang URL backend redirect ngắn cho GCash.
- Không thêm approve step giữa `gcash_confirm` và `gcash_start` nếu không có evidence mới.
- Không coi `QR_READY` là Plus; phải check plan.
- Không đóng browser ngay sau khi lấy QR; phải giữ sống đủ lâu để callback.
- Link copy cho GCash nên ưu tiên final URL `m.gcash.com/...` nếu browser resolve được, không phải link log bị rút gọn bằng `...`.
