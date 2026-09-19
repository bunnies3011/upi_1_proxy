# UPI Direct Security Notes

UPI Direct (`payment_method=upi_direct`) is an in-house ChatGPT + Stripe UPI QR
flow. It does **not** use third-party payment vendors (Capybara / OaiPay).

## Trusted-local boundary

The HTTP API remains **trusted-local-only**. Log redaction and proxy masking do
**not** replace authentication. Do not expose the service on an untrusted
network without an external auth layer.

## Egress allowlist

| Host | Purpose |
|------|---------|
| `chatgpt.com` | Checkout, promo update, approve, entitlement |
| `auth.openai.com` / `sentinel.openai.com` | Login (via `_chatgpt`) |
| `api.stripe.com` | Payment page init / elements / confirm / refresh |
| `js.stripe.com` | Token config bundle |
| `qr.stripe.com` | PNG QR image only (`*.png`, HTTPS/443) |
| `payments.stripe.com` | Hosted UPI instructions (`/upi/instructions/…`) |

Configured operator proxies (A/B and core lease) are additional egress by design.

## Proxy roles

| Role | Setting | Traffic |
|------|---------|---------|
| Login | Core `proxy_lease` / DIRECT | ChatGPT login only; explicit lease health |
| A | `upi_direct.proxy_checkout` | Checkout, Stripe, approve retry, QR fetch |
| B | `upi_direct.proxy_promotion` | Promo update only (optional) |

A is required before checkout. B empty → skip promo with sanitized log.

## Secrets lifecycle

Password, TOTP, access tokens, raw/materialized proxy credentials, SID, and
Stripe checksum/token fields are registered for value redaction before free-form
logs. Free-text also runs through `sanitize_proxy_text`.

## QR validation

- Exact UPI `next_action` path only (no fuzzy scraping).
- Approve POST can retry on ambiguous outcomes according to
  `upi_direct.approve_error_retries`; each ambiguous response is reconciled with
  one read-only refresh before the next POST.
- URL allowlist + non-blocking public-IP resolution before download; redirects
  disabled on the QR/hosted fetch.
- Hosted-instructions HTML: bounded (256 KiB); the extracted `upi:` URI is
  length-capped (2 KiB) and rejected if it contains whitespace/control chars —
  no raw, unbounded response text is ever rendered.
- PNG: magic, size (2 MiB), dimensions (4096²), re-encode via Pillow.
- Atomic publish: temp file + `os.replace` → `{job_id}.png`.
- Blocking work (DNS, Pillow decode/encode, `qrcode`, fsync write) runs off the
  event loop and races the cancellation token so a stop/timeout stays responsive.

## Known residual risks

These are accepted, documented gaps — not silent omissions. Revisit if the
threat model changes (e.g. the service is ever exposed beyond trusted-local).

- **QR-fetch DNS pinning is best-effort, not connection-bound.** The QR/hosted
  fetch runs through proxy A, so the proxy — not this process — performs the DNS
  resolution that the socket actually connects to. The pre-flight public-IP
  check therefore validates an address the connection may not reuse; client-side
  connection pinning is not achievable through an HTTP proxy. Effective controls
  for this path are the host allowlist, disabled redirects, byte/pixel caps, and
  Pillow re-encode. Direct (proxy-less) QR fetch is not a supported path.
- **Redirect allowlisting is scoped to the QR/hosted fetch only.** The login,
  checkout, promo, Stripe and approve clients follow redirects automatically
  (OAuth login in particular depends on 3xx). A compromised first-party endpoint
  could in principle redirect an authenticated request to another host. This is
  accepted under the inherited trusted-first-party (OpenAI/Stripe) threat model;
  full per-hop redirect allowlisting across every client is deferred hardening.

## Source quarantine

Code was ported from a read-only external tree treated as credential-contaminated.
Only reviewed schemas/logic were adapted; fixtures are synthetic.
