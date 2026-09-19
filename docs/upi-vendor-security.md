# UPI Vendor Security

Written 2026-07-12 against the current source tree (`backend/app/payments/upi/`).

The UPI payment method reuses the ChatGPT login flow but, instead of driving the
iDEAL -> Stripe -> `pay.ideal.nl` path, sends the resulting ChatGPT
`access_token` to a **third-party vendor host, `pix.capybara.cv`**, which returns
a UPI QR. This is a new egress posture relative to iDEAL and is the reason this
document exists. Treat enabling UPI in production as an explicit decision to share
a live ChatGPT session token with an external party.

## Data Egress

### What leaves the system

The ChatGPT `access_token` is a bearer JWT (`iss=auth.openai.com`, roughly
1.8 KB). It authenticates as the logged-in ChatGPT account for the lifetime of
the token. Under iDEAL it is only ever sent to OpenAI/Stripe/`pay.ideal.nl`.
Under UPI it is transmitted to `pix.capybara.cv`.

### Where it goes

`CapybaraClient` (`payments/upi/vendor_client.py`) is the only module that knows
the vendor wire protocol. It calls four endpoints on the configured host
(`upi.vendor_base_url`, default `https://pix.capybara.cv`):

| Endpoint | Body carrying the token | Purpose |
| --- | --- | --- |
| `POST /api/account/check` | `{accessToken}` | Optional eligibility precheck |
| `POST /api/v1/key/verify` | `{codes, channel}` (no token) | License credit lookup |
| `POST /api/chatgpt/challenge` | `{code}` (no token) | Anti-abuse nonce |
| `POST /api/chatgpt/run` | `{accessToken, code, challenge}` | Stream UPI QR (NDJSON) |

The token leaves the system on `account/check` (only when the precheck is
enabled) and on every `run`. `key/verify` and `challenge` do not carry the token.
The vendor holds the token for as long as it retains request logs — outside our
control. If the vendor is compromised or malicious, it can act as the ChatGPT
account until the token expires or is revoked.

## Redaction Guarantees

Redaction is **value-based**. Before the first vendor call, the handler
(`payments/upi/flow.py`) adds the `access_token` VALUE (plus the account
`password`/`totp_secret` when present) to a per-job `known_secrets` list. Every
free-form log line and error string produced by the handler and the vendor client
is passed through `redact_message(msg, known_secrets)`
(`core/redaction.py`), which replaces each secret value with `***REDACTED***`.

Guarantees:

- The token value is stripped from all logs and error messages, including vendor
  error details and NDJSON progress lines surfaced over SSE.
- The **raw vendor request body is never logged.** `run` sends the token inside
  the JSON body and only ever logs `"upi vendor run: opening NDJSON stream"` and
  the response status — never the body. The unary helper `_post_json` likewise
  logs only the endpoint name and status, never the request body.
- **Key-name masking (`redact_dict`) is deliberately not relied upon here.**
  `redact_dict` masks dict values whose KEY matches a sensitive-name set that
  contains snake_case `access_token`, not the vendor's camelCase field
  `accessToken`. Relying on it would silently leak the token if a body were ever
  logged. Value-based `redact_message` catches the token regardless of the field
  name, so it is the load-bearing control.
- **`core/` redaction is untouched.** UPI reuses `core/redaction.py` at its own
  error boundary; no core redaction rule was changed for UPI.

A unit test (`tests/unit/test_upi_flow_handler.py::
test_access_token_redacted_in_error_message`) and the integration test assert the
token value never appears in captured logs or error messages.

## Third-Party Blast Radius

The tool's single largest dependency risk is the vendor. Failure modes and their
containment:

- **Vendor down / network error / non-2xx** -> the affected job fails with a
  stable error code (see table below); other payment methods are unaffected.
- **Vendor schema change** (endpoint paths, JSON field names, NDJSON shape) ->
  contained to a single file. `CapybaraClient` + `payments/upi/models.py` are the
  only places that encode the wire schema, so a vendor change is a single-file
  swap, not a cross-cutting edit.
- **Vendor rate-limit / ban risk** -> bounded by `upi.max_concurrent`
  (default **3**, hard cap 10), a concurrency budget **separate from iDEAL**.
  UPI cannot starve iDEAL slots and vice-versa, and the low default keeps vendor
  request volume conservative given long (45-90s) runs.
- **Pause mid-stream is out of scope.** Only cancellation interrupts an in-flight
  `run` stream; `is_paused()` is not honoured mid-run.

### Error codes

The stable codes surfaced to the API/logs (source of truth:
`payments/upi/errors.py`):

| Error code | Raised by | Meaning |
| --- | --- | --- |
| `upi_no_license_credit` | `LicenseExhaustedError` | Pool empty / all codes exhausted (hard fail, no auto-retry) |
| `upi_license_error` | `LicenseError` | `key/verify` transport/HTTP/parse failure |
| `upi_challenge_error` | `ChallengeError` | `challenge` failed or returned an unusable payload |
| `upi_ineligible` | `EligibilityError` | `account/check` precheck reported the account ineligible |
| `upi_run_failed` | `VendorRunError` | `run` completed with `result.ok=false` (business failure) |
| `upi_stream_error` | `VendorStreamError` | NDJSON stream truncated/malformed/transport-broken |
| `upi_run_cancelled` | `VendorRunCancelled` | Cancellation interrupted the stream -> job `STOPPED` |
| `upi_run_timeout` | `VendorTimeoutError` | `upi.run_timeout_seconds` wall-clock cap elapsed |
| `upi_qr_decode_failed` | `QrDecodeError` | Vendor `qr_image_png` data URI could not be decoded |

## License Credit Handling

License credit lives in `UpiLicensePool` (`payments/upi/license_pool.py`), a
RAM-cached pool of `PK-XXXX` codes, refreshed from `key/verify`:

- **Exhausted codes are auto-removed** from the active pool with a `WARNING` log
  (both when `key/verify` reports no remaining credit and when a `run` fails with
  a credit-specific vendor code, via `mark_exhausted`).
- **An empty pool is a hard error:** `acquire()` raises `LicenseExhaustedError`
  -> `JobResult(ERROR, error_code="upi_no_license_credit")`. There is **no
  auto-retry** for this durable failure; an operator must re-list codes to refill.
- **No Telegram alert and no low-credit threshold.** Low-credit alerting was
  intentionally dropped; the only signal is the removal `WARNING` and the hard
  error when the pool empties.

### Open question: does a failed run consume a credit?

Credit is **reserved at `acquire()`** and **restored on any non-success terminal**
via `pool.release(committed=False)` (ineligible, challenge fail, stream error,
cancel, timeout, QR-decode fail). This is correct **only if the vendor does not
charge a credit on a failed `run`.** Whether it does is **unconfirmed** — it was
not determinable from the vendor HAR. The current default is
**restore-on-failure**, pending a live probe with a spare `PK` code. If a probe
shows the vendor charges on failure, the release policy for the relevant terminals
must change to `committed=True`.

## Operator Guidance

- **`upi.eligibility_precheck` (default OFF).** When ON, the handler calls
  `account/check` before reserving a credit, so an ineligible account fails fast
  with `upi_ineligible` and burns neither a license credit nor a 45-90s `run`.
  The trade-off: the precheck **still egresses the `access_token`** to the vendor
  (`account/check {accessToken}`). With it OFF, an ineligible account is only
  discovered after a full `run` (a wasted credit and wait), but the token reaches
  the vendor only at `run` time. Enable it to save credits/time; leave it off to
  minimise the number of distinct calls that carry the token.
- **`upi.vendor_base_url`.** Points at the vendor host; the single knob to swap or
  disable the egress target.
- **`upi.license_codes`.** The `PK-XXXX` pool. `key/verify` egresses directly from
  the server IP (not proxied), at a low lazy/refresh cadence.

## Recommendation

Do not enable UPI on an internet-exposed deployment without accepting that live
ChatGPT session tokens are shared with `pix.capybara.cv`. Keep `max_concurrent`
conservative, monitor the removal `WARNING`s, and resolve the failed-run credit
question with a live probe before relying on the pool accounting.
