"""QuickJS-driven Sentinel token generator.

Runs OpenAI's real sdk.js inside a Node subprocess to produce tokens that pass
deep server-side verification (required for OTP dispatch to actually send emails).

Adapted from https://github.com/Regert888/gpt-outlook-register (sentinel_quickjs.py)
and https://github.com/zc-zhangchen/any-auto-register (MIT License).

Two passes:
  1. action=requirements → request_p (fingerprint token)
  2. POST /sentinel/req with request_p → challenge (server token + PoW params)
  3. action=solve with challenge → final_p + t + so (solved enforcement token
     + turnstile VM proof + session-observer VM proof)
  4. Assemble {p: final_p, t, c: server_token, id: device_id, flow} → JSON string

Challenge của sentinel là **single-use**: `t` và `so` bắt buộc phải sinh ra từ
cùng một lần fetch challenge, nên cặp token phải mint trong 1 lượt
(`get_sentinel_pair_via_quickjs`), không thể gọi 2 lần rồi ghép lại.

Public API:
    get_sentinel_token_via_quickjs(session, device_id, flow, ...) -> str | None
    get_sentinel_pair_via_quickjs(session, device_id, flow, ...)
        -> (sentinel_token | None, so_token | None, diagnostics)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from app.payments._chatgpt.user_agent_profile import (
    BrowserPersona as _BrowserPersona,
    CHROME_145_WIN as _DEFAULT_PERSONA,
    sentinel_navigator_payload as _navigator_payload,
)

logger = logging.getLogger(__name__)

SENTINEL_VERSION = "20260219f9f6"
SENTINEL_SDK_URL = f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"

# TLS-library recovery (mirror sentinel_pow): curl_cffi/BoringSSL có thể corrupt
# state khi đổi host (login → sentinel.openai.com) hoặc chạy concurrent. Retry
# trên Session tươi thay vì raise → fallback PoW (vốn cũng dùng session corrupt).
_TLS_RETRY_MAX = 2


def _is_tls_library_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    markers = (
        "invalid library", "openssl_internal", "tls connect error",
        "curl: (35)", "curl: (56)", "curl: (7)", "sslerror", "handshake",
    )
    return any(m in msg for m in markers)


def _make_fresh_session(template_session: Any) -> Any:
    """Session curl_cffi tươi (clear corrupt BoringSSL), copy proxy từ session gốc."""
    from curl_cffi import requests as _curl_requests
    from app.payments._chatgpt.user_agent_profile import (
        CURL_IMPERSONATE_PRIMARY as _IMPERSONATE,
    )

    fresh = _curl_requests.Session(impersonate=_IMPERSONATE)
    fresh.trust_env = False
    try:
        proxy = getattr(template_session, "proxy", None)
        if proxy:
            fresh.proxy = proxy
        proxies = getattr(template_session, "proxies", None)
        if proxies:
            fresh.proxies = dict(proxies)
    except Exception:  # noqa: BLE001
        pass
    return fresh


def _resolve_node_binary() -> str:
    return (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"


def _quickjs_script_path() -> Path:
    return Path(__file__).resolve().parent / "openai_sentinel_quickjs.js"


def _ensure_sdk_file(
    session: Any,
    timeout_ms: int,
    *,
    persona: Optional[_BrowserPersona] = None,
) -> Path:
    """Download OpenAI's sdk.js to /tmp cache (one-shot per version).

    Headers theo persona — Chrome có sec-ch-ua, Firefox không (Task 3.2).
    """
    cache_dir = Path(tempfile.gettempdir()) / "openai-sentinel-demo" / SENTINEL_VERSION
    cache_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = cache_dir / "sdk.js"
    if sdk_file.exists() and sdk_file.stat().st_size > 0:
        return sdk_file

    p = persona or _DEFAULT_PERSONA
    headers = p.common_headers(referer="https://auth.openai.com/")
    headers.update({
        "accept": "*/*",
        "sec-fetch-dest": "script",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "same-site",
    })
    # Lowercase versions cũng giữ (curl_cffi case-insensitive nhưng để đồng bộ
    # với code cũ → headers có cả "User-Agent" capital + "user-agent" lowercase
    # đều được — curl_cffi normalize).

    resp = session.get(
        SENTINEL_SDK_URL,
        headers=headers,
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"Download sdk.js failed: HTTP {resp.status_code}")
    content = getattr(resp, "content", b"") or (resp.text or "").encode()
    if not content:
        raise RuntimeError("Download sdk.js failed: empty response")
    sdk_file.write_bytes(content)
    return sdk_file


_WRAPPER_JS = """
const fs = require('fs');
const timeoutMs = Number(process.env.OPENAI_SENTINEL_VM_TIMEOUT_MS || '10000');
const sdkFile = process.env.OPENAI_SENTINEL_SDK_FILE;
const scriptFile = process.env.OPENAI_SENTINEL_QUICKJS_SCRIPT;

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    const payload = JSON.parse(input || '{}');
    globalThis.__payload_json = JSON.stringify(payload);
    globalThis.__sdk_source = fs.readFileSync(sdkFile, 'utf8');
    globalThis.__vm_done = false;
    globalThis.__vm_output_json = '';
    globalThis.__vm_error = '';
    const script = fs.readFileSync(scriptFile, 'utf8');
    eval(script);

    const started = Date.now();
    while (!globalThis.__vm_done) {
      if ((Date.now() - started) > timeoutMs) {
        throw new Error('QuickJS script timeout');
      }
      await new Promise((resolve) => setTimeout(resolve, 1));
    }

    if (String(globalThis.__vm_error || '').trim()) {
      throw new Error(String(globalThis.__vm_error));
    }

    process.stdout.write(String(globalThis.__vm_output_json || ''));
  } catch (err) {
    const msg = err && err.stack ? String(err.stack) : String(err);
    process.stderr.write(msg);
    process.exit(1);
  }
});
""".strip()


# ─── Persistent Node worker (warm process — tránh cold-start V8 mỗi action) ──

# Wrapper loop: đọc từng dòng JSON {id, action, sdk_file, script_file, payload,
# timeout_ms} từ stdin, xử lý y hệt _WRAPPER_JS (set globals → eval script →
# poll __vm_done), ghi 1 dòng JSON {id, ok, output|error} ra stdout. Tái dùng
# 1 Node process cho nhiều action → tiết kiệm V8/Node startup (~150-300ms/lần).
_WORKER_BOOTSTRAP_JS = r"""
const fs = require('fs');
const readline = require('readline');

// Giữ reference setTimeout GỐC trước khi sdk/installRuntime override nó thành
// synchronous — wrapper loop phải dùng timer thật để không kẹt event loop.
const _origSetTimeout = setTimeout;

// sdk source cache theo path (đọc 1 lần / version).
const _sdkCache = new Map();
function _loadSdk(file) {
  if (_sdkCache.has(file)) return _sdkCache.get(file);
  const src = fs.readFileSync(file, 'utf8');
  _sdkCache.set(file, src);
  return src;
}

// Chuyển mọi console.* sang stderr để stdout chỉ chứa protocol JSON.
const _toErr = (...a) => { try { process.stderr.write(a.map(String).join(' ') + '\n'); } catch (e) {} };
console.log = _toErr; console.info = _toErr; console.warn = _toErr;
console.error = _toErr; console.debug = _toErr;

const rl = readline.createInterface({ input: process.stdin });

rl.on('line', async (line) => {
  const trimmed = (line || '').trim();
  if (!trimmed) return;
  let job;
  try { job = JSON.parse(trimmed); } catch (e) { return; }
  const id = job.id;
  const timeoutMs = Number(job.timeout_ms || 10000);
  try {
    const sdkSource = _loadSdk(job.sdk_file);
    const scriptSource = fs.readFileSync(job.script_file, 'utf8');
    const payload = job.payload || {};
    payload.action = job.action;

    globalThis.__payload_json = JSON.stringify(payload);
    globalThis.__sdk_source = sdkSource;
    globalThis.__vm_done = false;
    globalThis.__vm_output_json = '';
    globalThis.__vm_error = '';

    eval(scriptSource);

    const started = Date.now();
    while (!globalThis.__vm_done) {
      if ((Date.now() - started) > timeoutMs) throw new Error('QuickJS script timeout');
      await new Promise((resolve) => _origSetTimeout(resolve, 1));
    }
    if (String(globalThis.__vm_error || '').trim()) {
      throw new Error(String(globalThis.__vm_error));
    }
    process.stdout.write(JSON.stringify({ id: id, ok: true, output: String(globalThis.__vm_output_json || '') }) + '\n');
  } catch (err) {
    const msg = err && err.stack ? String(err.stack) : String(err);
    process.stdout.write(JSON.stringify({ id: id, ok: false, error: msg }) + '\n');
  }
});
""".strip()


class SentinelNodeWorker:
    """Persistent Node process cho sentinel — tái dùng qua nhiều action.

    Giao tiếp line-protocol qua stdin/stdout (1 dòng JSON/request, 1 dòng
    JSON/response). Tuần tự hóa bằng lock vì 1 reg có thể gọi từ thread chính
    (sentinel #1) và thread pre-compute (sentinel #2) — tuy không overlap nhưng
    lock đảm bảo an toàn. Tự respawn nếu process chết.
    """

    def __init__(self, *, node_path: str, script_file: Path, log: Callable[[str], None]) -> None:
        self._node = node_path
        self._script = str(script_file)
        self._log = log
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._counter = 0

    def _ensure_proc(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            [self._node, "-e", _WORKER_BOOTSTRAP_JS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # tránh deadlock khi stderr buffer đầy
            text=True,
            bufsize=1,
            env={**os.environ},
        )

    def run_action(
        self,
        *,
        action: str,
        sdk_file: Path,
        payload: dict,
        timeout_ms: int,
    ) -> dict:
        with self._lock:
            self._ensure_proc()
            proc = self._proc
            assert proc is not None and proc.stdin is not None and proc.stdout is not None

            self._counter += 1
            req_id = self._counter
            job = {
                "id": req_id,
                "action": action,
                "sdk_file": str(sdk_file),
                "script_file": self._script,
                "payload": dict(payload),
                "timeout_ms": min(timeout_ms, 30000),
            }
            try:
                proc.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise RuntimeError(f"sentinel worker stdin write failed: {exc}") from exc

            deadline = time.monotonic() + max(10.0, timeout_ms / 1000 + 5)
            while time.monotonic() < deadline:
                out = proc.stdout.readline()
                if not out:
                    raise RuntimeError("sentinel worker stdout closed (process died)")
                out = out.strip()
                if not out:
                    continue
                try:
                    resp = json.loads(out)
                except Exception:
                    continue  # bỏ qua dòng noise không phải protocol
                if not isinstance(resp, dict) or resp.get("id") != req_id:
                    continue
                if not resp.get("ok"):
                    raise RuntimeError(
                        f"sentinel worker action={action} failed: "
                        f"{str(resp.get('error'))[:300]}"
                    )
                data = json.loads(resp.get("output") or "{}")
                if not isinstance(data, dict):
                    raise RuntimeError("sentinel worker output is not a JSON object")
                return data
            raise RuntimeError(f"sentinel worker timeout (action={action})")

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def create_worker(log: Optional[Callable[[str], None]] = None) -> Optional["SentinelNodeWorker"]:
    """Tạo persistent Node worker. None nếu script không tồn tại."""
    log = log or (lambda m: logger.info(m))
    script = _quickjs_script_path()
    if not script.exists():
        return None
    return SentinelNodeWorker(
        node_path=_resolve_node_binary(),
        script_file=script,
        log=log,
    )


def _run_quickjs_action(
    *,
    action: str,
    sdk_file: Path,
    quickjs_script: Path,
    payload: dict,
    timeout_ms: int,
) -> dict:
    body = dict(payload)
    body["action"] = action
    proc = subprocess.run(
        [_resolve_node_binary(), "-e", _WRAPPER_JS],
        input=json.dumps(body, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=max(10, int(timeout_ms / 1000) + 5),
        env={
            **os.environ,
            "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
            "OPENAI_SENTINEL_QUICKJS_SCRIPT": str(quickjs_script),
            "OPENAI_SENTINEL_VM_TIMEOUT_MS": str(min(timeout_ms, 30000)),
        },
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"QuickJS failed: {(proc.stderr or proc.stdout or 'unknown').strip()[:300]}"
        )
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("QuickJS returned empty output")
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("QuickJS output is not a JSON object")
    return data


def _fetch_sentinel_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    timeout_ms: int,
    persona: Optional[_BrowserPersona] = None,
) -> dict:
    """POST /sentinel/req → challenge dict. Headers theo persona (Task 3.2)."""
    p = persona or _DEFAULT_PERSONA
    body = {"p": request_p, "id": device_id, "flow": flow}

    # Build headers theo persona — Chrome có sec-ch-ua, Firefox không.
    _headers: dict[str, str] = {
        "origin": "https://sentinel.openai.com",
        "referer": (
            f"https://sentinel.openai.com/backend-api/sentinel/frame.html"
            f"?sv={SENTINEL_VERSION}"
        ),
        "content-type": "text/plain;charset=UTF-8",
        "accept": "*/*",
        "accept-encoding": p.accept_encoding,
        "accept-language": p.accept_language,
        "user-agent": p.user_agent,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if p.sec_ch_ua:
        _headers["sec-ch-ua"] = p.sec_ch_ua
        if p.sec_ch_ua_mobile:
            _headers["sec-ch-ua-mobile"] = p.sec_ch_ua_mobile
        if p.sec_ch_ua_platform:
            _headers["sec-ch-ua-platform"] = p.sec_ch_ua_platform
    _data = json.dumps(body, separators=(",", ":"))
    _timeout = max(10, int(timeout_ms / 1000))

    # Attempt trên session gốc; nếu TLS-library corruption → retry fresh session.
    # Lỗi khác (HTTP, JSON) propagate như cũ (RuntimeError ở dưới / caller catch).
    resp = None
    for attempt in range(_TLS_RETRY_MAX + 1):
        active = session if attempt == 0 else _make_fresh_session(session)
        try:
            resp = active.post(SENTINEL_REQ_URL, data=_data, headers=_headers, timeout=_timeout)
            break
        except Exception as exc:  # noqa: BLE001
            if not _is_tls_library_error(exc) or attempt >= _TLS_RETRY_MAX:
                raise
            logger.warning(
                "QuickJS /sentinel/req TLS-library error (retry %d/%d) → fresh session: %s",
                attempt + 1, _TLS_RETRY_MAX, exc,
            )
        finally:
            if attempt > 0 and active is not session:
                try:
                    active.close()
                except Exception:  # noqa: BLE001
                    pass
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"/sentinel/req HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Sentinel challenge response is not a JSON object")
    return payload


def _quickjs_solve_once(
    session: Any,
    device_id: str,
    *,
    flow: str,
    timeout_ms: int,
    worker: Optional["SentinelNodeWorker"] = None,
    persona: Optional[_BrowserPersona] = None,
) -> dict:
    """Chạy trọn 3 pass QuickJS trên MỘT challenge → giá trị thô.

    Challenge sentinel là **single-use**, nên cả ``t`` (turnstile) lẫn ``so``
    (session-observer) phải lấy từ cùng một lần fetch. Đây là chỗ duy nhất
    thực hiện chuỗi requirements → /sentinel/req → solve; hai public API bên
    dưới chỉ khác nhau ở cách đóng gói kết quả.

    Returns:
        ``{"did", "c", "final_p", "t", "so", "so_error", "t_suspect",
        "challenge"}``. ``t``/``so`` là chuỗi rỗng khi server không yêu cầu
        (hoặc VM tương ứng trả null). ``so_error`` mang thông báo lỗi đã giải mã
        của VM session-observer (VM resolve chứ không reject khi lỗi, nên đây là
        cách duy nhất phân biệt "không cần so" với "so hỏng"); ``t_suspect``
        True khi ``t`` toàn chữ số — dấu hiệu guard 500ms của SDK thắng race vì
        timer bị stub đồng bộ.

    Raises:
        RuntimeError và mọi exception từ Node / HTTP / JSON. Caller tự quyết
        định degrade (trả None) hay propagate.
    """
    quickjs_script = _quickjs_script_path()
    if not quickjs_script.exists():
        raise RuntimeError(f"QuickJS script not found: {quickjs_script}")

    did = str(device_id or uuid.uuid4())
    # Navigator persona pass vào sdk.js — khớp UA HTTP + sec-ch-ua + hardware.
    # Trước refactor không pass → sdk.js thấy navigator.userAgent="Mozilla/5.0"
    # (default trong JS Node context) → fingerprint cực generic, fail deep verification.
    nav_payload = _navigator_payload(persona)
    # sdk.js lấy config[5] = 1 `src` ngẫu nhiên trong ``document.scripts``. Trang
    # thật load sdk.js theo URL CÓ version, nên seed đúng URL đang dùng thay vì
    # hardcode trong JS (tránh lệch build khi SENTINEL_VERSION xoay).
    nav_payload["sdk_url"] = SENTINEL_SDK_URL
    sdk_file = _ensure_sdk_file(session, timeout_ms, persona=persona)

    def _action(action: str, payload: dict) -> dict:
        if worker is not None:
            return worker.run_action(
                action=action,
                sdk_file=sdk_file,
                payload=payload,
                timeout_ms=timeout_ms,
            )
        return _run_quickjs_action(
            action=action,
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload=payload,
            timeout_ms=timeout_ms,
        )

    # Pass 1: generate requirements token (fingerprint)
    requirements = _action("requirements", {"device_id": did, **nav_payload})
    request_p = str(requirements.get("request_p") or "").strip()
    if not request_p:
        raise RuntimeError("QuickJS requirements did not return request_p")

    # Pass 2: fetch challenge from server
    challenge = _fetch_sentinel_challenge(
        session, device_id=did, flow=flow, request_p=request_p,
        timeout_ms=timeout_ms, persona=persona,
    )
    c_value = str(challenge.get("token") or "").strip()
    if not c_value:
        raise RuntimeError("Challenge token is empty")

    # Pass 3: solve challenge → p + turnstile proof + session-observer proof
    solved = _action(
        "solve",
        {
            "device_id": did,
            "request_p": request_p,
            "challenge": challenge,
            **nav_payload,
        },
    )
    final_p = str(solved.get("final_p") or solved.get("p") or "").strip()
    if not final_p:
        raise RuntimeError("QuickJS solve did not return final_p")

    def _clean(key: str) -> str:
        raw = solved.get(key)
        return "" if raw is None else str(raw).strip()

    return {
        "did": did,
        "c": c_value,
        "final_p": final_p,
        "t": _clean("t"),
        "so": _clean("so"),
        "so_error": _clean("so_error") or None,
        "t_suspect": bool(solved.get("t_suspect")),
        "challenge": challenge,
    }


def _challenge_requirements(challenge: dict) -> tuple[bool, bool]:
    """Đọc challenge → ``(turnstile_required, so_required)``.

    Mirror pay153 ``sentinel_token.py`` — turnstile coi là bắt buộc khi server
    set cờ ``required`` HOẶC gửi kèm ``dx`` (có dx nghĩa là có việc phải giải).
    """
    turnstile = challenge.get("turnstile")
    turnstile = turnstile if isinstance(turnstile, dict) else {}
    so_info = challenge.get("so")
    so_info = so_info if isinstance(so_info, dict) else {}
    return (
        bool(turnstile.get("required") or turnstile.get("dx")),
        bool(so_info.get("required") or so_info.get("collector_dx")),
    )


def get_sentinel_token_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
    worker: Optional["SentinelNodeWorker"] = None,
    persona: Optional[_BrowserPersona] = None,
) -> Optional[str]:
    """Run QuickJS sentinel path. Returns JSON string on success, None on failure.

    Caller should fall back to sentinel_pow.get_sentinel_token() on None.

    Chỉ trả về ``openai-sentinel-token``. Luồng cần cả so-token (payment /
    checkout) dùng ``get_sentinel_pair_via_quickjs`` — một challenge, hai token.

    Args:
        persona: BrowserPersona để inject vào sdk.js navigator + HTTP headers.
            None = backward compat = CHROME_145_WIN (Task 3.2). Caller mới nên
            truyền explicit (vd ``CHROME_145_WIN`` cho pure_request login).

    Nếu ``worker`` được truyền → chạy action qua persistent Node process (warm,
    tránh cold-start). Nếu None → spawn Node one-shot mỗi action (hành vi cũ).
    """
    log = log or (lambda m: logger.info(m))
    try:
        solved = _quickjs_solve_once(
            session, device_id, flow=flow, timeout_ms=timeout_ms,
            worker=worker, persona=persona,
        )
    except Exception as e:
        log(f"[sentinel] QuickJS error: {e}")
        return None

    final_p, t_value, c_value = solved["final_p"], solved["t"], solved["c"]
    if not t_value:
        log("[sentinel] QuickJS solve did not return valid t")
        return None

    token = json.dumps(
        {"p": final_p, "t": t_value, "c": c_value, "id": solved["did"], "flow": flow},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    log(f"[sentinel] QuickJS OK (p={len(final_p)} t={len(t_value)} c={len(c_value)})")
    return token


def get_sentinel_pair_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "chatgpt_checkout",
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
    worker: Optional["SentinelNodeWorker"] = None,
    persona: Optional[_BrowserPersona] = None,
) -> tuple[Optional[str], Optional[str], dict]:
    """Mint CẶP sentinel token + so-token từ MỘT challenge duy nhất.

    Checkout của ChatGPT cần đủ 2 header — ``openai-sentinel-token`` và
    ``openai-sentinel-so-token`` — và cả hai phải mang **cùng một** giá trị
    ``c`` (challenge single-use, mint 2 lần riêng sẽ lệch ``c`` → server từ chối).

    Khác ``get_sentinel_token_via_quickjs``: ``t`` rỗng KHÔNG bị coi là fail khi
    server không yêu cầu turnstile — token được emit không có key ``t``, đúng
    như SDK thật. Chỉ fail khi thiếu ``p`` hoặc ``c``.

    Returns:
        ``(sentinel_token, so_token, diagnostics)``. ``sentinel_token`` là None
        khi mint fail; ``so_token`` là None khi server không yêu cầu so hoặc VM
        session-observer không trả proof. ``diagnostics`` luôn là dict:
        ``{flow, turnstile_required, so_required, has_t, has_so, init_error,
        so_error, t_suspect}``.
    """
    log = log or (lambda m: logger.info(m))
    diagnostics: dict[str, Any] = {
        "flow": flow,
        "turnstile_required": False,
        "so_required": False,
        "has_t": False,
        "has_so": False,
        "init_error": None,
        "so_error": None,
        "t_suspect": False,
    }

    try:
        solved = _quickjs_solve_once(
            session, device_id, flow=flow, timeout_ms=timeout_ms,
            worker=worker, persona=persona,
        )
    except Exception as e:
        diagnostics["init_error"] = f"{type(e).__name__}: {e}"
        log(f"[sentinel] QuickJS pair error: {e}")
        return None, None, diagnostics

    did, c_value = solved["did"], solved["c"]
    final_p, t_value, so_value = solved["final_p"], solved["t"], solved["so"]

    turnstile_required, so_required = _challenge_requirements(solved["challenge"])
    diagnostics.update(
        turnstile_required=turnstile_required,
        so_required=so_required,
        has_t=bool(t_value),
        has_so=bool(so_value),
        so_error=solved.get("so_error"),
        t_suspect=bool(solved.get("t_suspect")),
    )

    payload: dict[str, str] = {"p": final_p, "c": c_value, "id": did, "flow": flow}
    if t_value:
        payload["t"] = t_value
    token = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    so_token: Optional[str] = None
    if so_value:
        so_token = json.dumps(
            {"so": so_value, "c": c_value, "id": did, "flow": flow},
            separators=(",", ":"),
            ensure_ascii=False,
        )

    # Cảnh báo khi server ĐÒI nhưng ta KHÔNG có — token vẫn gửi đi (best-effort),
    # nhưng operator cần thấy để biết sdk.js đã xoay build hay VM hỏng.
    if turnstile_required and not t_value:
        log(f"[sentinel] WARN: turnstile required nhưng thiếu t (flow={flow})")
    elif diagnostics["t_suspect"]:
        # `t` toàn chữ số = counter `kn` do guard 500ms của `On()` trả về, không
        # phải proof. Xảy ra khi `setTimeout` trong runtime bị stub đồng bộ.
        log(
            f"[sentinel] WARN: t={t_value!r} trông như counter guard, không phải "
            f"proof (flow={flow}) — kiểm tra shim setTimeout trong "
            f"openai_sentinel_quickjs.js"
        )
    if so_required and not so_value:
        detail = solved.get("so_error") or "VM không trả proof"
        log(
            f"[sentinel] WARN: so required nhưng thiếu so-token (flow={flow}): "
            f"{detail}"
        )

    log(
        f"[sentinel] QuickJS pair OK (p={len(final_p)} t={len(t_value)} "
        f"so={len(so_value)} c={len(c_value)} flow={flow})"
    )
    return token, so_token, diagnostics
