"""Backend package.

HTTP layer dùng `curl_cffi` (BoringSSL bundled) — không đi qua Python
`ssl` module, nên KHÔNG cần `truststore.inject_into_ssl()` như bản `httpx`.
Nếu vẫn cần OS trust store cho code path khác (không tồn tại hiện tại),
add lại ở đây trước bất kỳ import nào chạm mạng.
"""
from __future__ import annotations
