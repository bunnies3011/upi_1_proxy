"""Private Telegram bot for the current GCash Direct flow.

This is a separate bot process, not the existing Telegram notifier/group flow.
Usage is one-on-one:

    1. Create a new bot with BotFather and copy its token.
    2. Run this file with TELEGRAM_BOT_TOKEN set.
    3. Open that bot in Telegram, press /start.
    4. Send one line: email|password|totp_secret
    5. The bot runs GcashDirectFlowHandler directly and returns QR/link.

Environment:
    TELEGRAM_BOT_TOKEN                 required
    GCASH_PRIVATE_BOT_DB_PATH          default backend/runtime/ideal_qr_tool.db
    GCASH_PRIVATE_BOT_SESSION_CACHE    default backend/runtime/session_cache
    GCASH_PRIVATE_BOT_QR_DIR           default backend/runtime/qr
    GCASH_PRIVATE_BOT_DELETE_INPUT     default true
    GCASH_PRIVATE_BOT_REQUIRE_PROXY    default true
    GCASH_PRIVATE_BOT_HTTP_TIMEOUT     default 75
    GCASH_PRIVATE_BOT_SUCCESS_TIMEOUT  default 300
    GCASH_PRIVATE_BOT_SUCCESS_INTERVAL default 8
    GCASH_PRIVATE_BOT_EVENT_LOOP       default proactor on Windows
    GCASH_PRIVATE_BOT_PROXY_PROBE      default true
    GCASH_PRIVATE_BOT_PROXY_PROBE_TIMEOUT default 20
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import html
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


if sys.platform == "win32":
    _loop_policy = os.environ.get("GCASH_PRIVATE_BOT_EVENT_LOOP", "proactor").strip().lower()
    if _loop_policy == "selector":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    else:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

REPO_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core import http_client as http
from app.core.db import DbEngine
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    SimpleCancellationToken,
)
from app.core.proxy_format import mask_proxy, materialize_proxy
from app.core.proxy_pool import ProxyLease
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments.gcash_direct import register_gcash_direct_namespace
from app.payments.gcash_direct.flow import GcashDirectFlowHandler


DEFAULT_DB_PATH = BACKEND_ROOT / "runtime" / "ideal_qr_tool.db"
DEFAULT_SESSION_CACHE_DIR = BACKEND_ROOT / "runtime" / "session_cache"
DEFAULT_QR_DIR = BACKEND_ROOT / "runtime" / "qr"


class BotError(Exception):
    pass


class TelegramHttpError(BotError):
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"http_{status}:{_short_text(body)}")


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: Path
    session_cache_dir: Path
    qr_dir: Path
    delete_input: bool
    require_proxy: bool
    http_timeout_seconds: int
    success_timeout_seconds: int
    success_interval_seconds: int
    proxy_probe: bool
    proxy_probe_timeout_seconds: int


@dataclass(frozen=True)
class BotInput:
    account_line: str
    email: str
    proxy_line: str | None


class BotSettings:
    """Per-job settings overlay for the private bot.

    The web tool may already have stale/bad `gcash_direct.proxy_*` values in
    SQLite. For this private bot, a user-provided proxy must be the only GCash
    proxy used by the job.
    """

    def __init__(
        self,
        base: SettingsRepository,
        proxy_line: str | None,
        http_timeout_seconds: int,
    ) -> None:
        self._base = base
        self._proxy_line = proxy_line
        self._proxy_url = materialize_proxy(proxy_line) if proxy_line else None
        self._http_timeout_seconds = http_timeout_seconds

    async def get(self, key: str) -> Any:
        if key == "gcash_direct.proxy_promotion":
            return [self._proxy_url] if self._proxy_url else []
        if key == "gcash_direct.proxy_checkout":
            return []
        if key == "gcash_direct.stripe_request_timeout_seconds":
            return self._http_timeout_seconds
        return await self._base.get(key)

    async def list(self, namespace: str) -> dict[str, Any]:
        data = await self._base.list(namespace)
        if namespace == "gcash_direct":
            data["gcash_direct.proxy_promotion"] = (
                [self._proxy_url] if self._proxy_url else []
            )
            data["gcash_direct.proxy_checkout"] = []
            data["gcash_direct.stripe_request_timeout_seconds"] = (
                self._http_timeout_seconds
            )
        return data

    @property
    def proxy_url(self) -> str | None:
        return self._proxy_url


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call_json(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: int = 20,
    ) -> Any:
        raw = _request(
            "POST",
            f"{self.base_url}/{method}",
            json_payload=payload,
            timeout=timeout,
        )
        data = json.loads(raw.decode("utf-8"))
        if not data.get("ok"):
            raise BotError(f"telegram {method} failed: {data}")
        return data

    def send_message(self, chat_id: str, text: str) -> None:
        self.call_json(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def send_plain(self, chat_id: str, text: str) -> None:
        for chunk in _split_text(text, 3900):
            self.call_json(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )

    def send_photo(self, chat_id: str, png: bytes, caption: str) -> None:
        self.call_multipart(
            "sendPhoto",
            fields={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": ("gcash_qr.png", png, "image/png")},
        )

    def send_document(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str,
    ) -> None:
        self.call_multipart(
            "sendDocument",
            fields={"chat_id": chat_id, "caption": caption[:1024]},
            files={
                "document": (
                    filename,
                    content,
                    mimetypes.guess_type(filename)[0] or "application/octet-stream",
                )
            },
        )

    def call_multipart(
        self,
        method: str,
        *,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> Any:
        boundary = f"----gcash-private-bot-{uuid.uuid4().hex}"
        body_parts: list[bytes] = []
        for name, value in fields.items():
            body_parts.append(f"--{boundary}\r\n".encode("ascii"))
            body_parts.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                )
            )
            body_parts.append(str(value).encode("utf-8"))
            body_parts.append(b"\r\n")
        for name, (filename, content, content_type) in files.items():
            body_parts.append(f"--{boundary}\r\n".encode("ascii"))
            body_parts.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("ascii")
            )
            body_parts.append(content)
            body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode("ascii"))
        raw = _request(
            "POST",
            f"{self.base_url}/{method}",
            raw_body=b"".join(body_parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=60,
        )
        data = json.loads(raw.decode("utf-8"))
        if not data.get("ok"):
            raise BotError(f"telegram {method} failed: {data}")
        return data

    def delete_message(self, chat_id: str, message_id: int) -> None:
        self.call_json(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )


class GcashPrivateTelegramBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.telegram = TelegramClient(config.bot_token)
        self._workers: set[threading.Thread] = set()
        self._success_events: dict[str, threading.Event] = {}

    def run_forever(self) -> None:
        offset: int | None = None
        _log("gcash private telegram bot started")
        while True:
            try:
                updates = self.telegram.call_json(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 50,
                        "allowed_updates": ["message"],
                    },
                    timeout=60,
                ).get("result", [])
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self._handle_update(update)
            except KeyboardInterrupt:
                _log("bot stopped")
                return
            except Exception as exc:  # noqa: BLE001 - keep polling alive.
                _log(f"poll error: {type(exc).__name__}: {exc}")
                time.sleep(3)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        chat_type = str(chat.get("type") or "")
        text = str(message.get("text") or "").strip()
        message_id = message.get("message_id")

        if not chat_id or not text:
            return
        if chat_type != "private":
            self.telegram.send_message(
                chat_id,
                "Bot nay chi dung trong private chat. Hay mo DM voi bot roi gui account.",
            )
            return

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"} or text.lower() == "help":
            self._send_help(chat_id)
            return

        bot_input = _parse_bot_input(text)
        if bot_input is None:
            self.telegram.send_message(
                chat_id,
                (
                    "Sai format. Gui 1 trong 2 kieu:\n"
                    "<code>email|password|totp_secret|proxy</code>\n\n"
                    "Hoac 2 dong:\n"
                    "<code>email|password|totp_secret\n"
                    "hostname:port:username:password</code>"
                ),
            )
            return
        if self.config.require_proxy and not bot_input.proxy_line:
            self.telegram.send_message(
                chat_id,
                (
                    "Thieu proxy. Bot GCash private dang bat buoc proxy.\n"
                    "Gui:\n"
                    "<code>email|password|totp_secret|hostname:port:username:password</code>\n"
                    "Hoac proxy o dong 2."
                ),
            )
            return

        if self.config.delete_input and isinstance(message_id, int):
            try:
                self.telegram.delete_message(chat_id, message_id)
            except Exception as exc:  # noqa: BLE001
                _log(f"delete input failed: {type(exc).__name__}: {exc}")

        email = bot_input.email
        proxy_label = mask_proxy(bot_input.proxy_line) if bot_input.proxy_line else "direct"
        self.telegram.send_message(
            chat_id,
            (
                "Da nhan account "
                f"<b>{html.escape(_mask_email(email))}</b>\n"
                f"Proxy: <code>{html.escape(proxy_label)}</code>\n"
                "Dang chay GCash flow..."
            ),
        )
        worker = threading.Thread(
            target=self._run_account_thread,
            args=(chat_id, bot_input),
            daemon=True,
            name="gcash-private-job",
        )
        self._workers.add(worker)
        worker.start()
        threading.Thread(
            target=_join_and_cleanup,
            args=(worker, lambda: self._workers.discard(worker)),
            daemon=True,
        ).start()

    def _run_account_thread(self, chat_id: str, bot_input: BotInput) -> None:
        try:
            asyncio.run(self._run_flow_and_notify(chat_id, bot_input))
        except Exception as exc:  # noqa: BLE001
            self.telegram.send_message(
                chat_id,
                (
                    "Bot error: "
                    f"<code>{html.escape(type(exc).__name__)}: "
                    f"{html.escape(str(exc))}</code>"
                ),
            )
            _log(traceback.format_exc())

    async def _run_flow_and_notify(
        self,
        chat_id: str,
        bot_input: BotInput,
    ) -> None:
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.session_cache_dir.mkdir(parents=True, exist_ok=True)
        self.config.qr_dir.mkdir(parents=True, exist_ok=True)

        engine = DbEngine(self.config.db_path)
        try:
            await engine.init_schema()
            base_settings = SettingsRepository(engine)
            await register_gcash_direct_namespace(base_settings)

            session_cache = AccountSessionCache(base_settings, self.config.session_cache_dir)
            session_cache.apply_settings(await base_settings.list("session_cache"))

            logger_factory = _make_job_logger_factory(chat_id, self.telegram)
            parse_handler = GcashDirectFlowHandler(
                settings=BotSettings(
                    base_settings,
                    bot_input.proxy_line,
                    self.config.http_timeout_seconds,
                ),  # type: ignore[arg-type]
                session_cache=session_cache,
                qr_output_dir=self.config.qr_dir,
                logger_factory=logger_factory,
            )

            parsed = parse_handler.parse_account_line(bot_input.account_line)
            if isinstance(parsed, AccountLineError):
                raise BotError(f"invalid account line: {parsed.reason}")

            job_id = uuid.uuid4().hex
            success_event = threading.Event()
            self._success_events[job_id] = success_event
            logger_factory = _make_job_logger_factory(
                chat_id,
                self.telegram,
                bot_input.email,
                success_event,
            )
            self.telegram.send_message(chat_id, f"Job: <code>{job_id}</code>")
            _log(f"run gcash job={job_id} email={_mask_email(bot_input.email)}")
            _log(
                "bot proxy input="
                + (
                    mask_proxy(bot_input.proxy_line)
                    if bot_input.proxy_line
                    else "missing"
                )
            )
            job = Job(
                job_id=job_id,
                payment_method="gcash_direct",
                account_line=bot_input.account_line,
                created_at=time.time(),
                cancellation_token=SimpleCancellationToken(),
            )
            result: JobResult | None = None
            handler: GcashDirectFlowHandler | None = None
            settings: BotSettings | None = None
            proxy_attempts = _proxy_attempts(bot_input.proxy_line)
            for attempt_index, proxy_line in enumerate(proxy_attempts, start=1):
                settings = BotSettings(
                    base_settings,
                    proxy_line,
                    self.config.http_timeout_seconds,
                )
                handler = GcashDirectFlowHandler(
                    settings=settings,  # type: ignore[arg-type]
                    session_cache=session_cache,
                    qr_output_dir=self.config.qr_dir,
                    logger_factory=logger_factory,
                )
                proxy_lease = (
                    ProxyLease(
                        proxy_id=proxy_line,
                        materialized_url=settings.proxy_url,
                        leased_at=time.time(),
                    )
                    if proxy_line and settings.proxy_url
                    else None
                )
                if attempt_index > 1:
                    self.telegram.send_message(
                        chat_id,
                        (
                            "Thu lai proxy bang scheme khac: "
                            f"<code>{html.escape(mask_proxy(proxy_line))}</code>"
                        ),
                    )
                    _log(
                        "bot proxy retry="
                        + (mask_proxy(proxy_line) if proxy_line else "missing")
                    )
                if self.config.proxy_probe and settings.proxy_url:
                    probe_ok = await self._probe_proxy_before_login(
                        chat_id,
                        settings.proxy_url,
                    )
                    if not probe_ok:
                        result = JobResult(
                            status=JobStatus.ERROR,
                            error_code="proxy_probe_failed",
                            error_message="Proxy cannot reach chatgpt.com/auth/login",
                        )
                        if attempt_index < len(proxy_attempts):
                            continue
                        break
                result = await handler.run(job, proxy_lease=proxy_lease)
                if result.status == JobStatus.QR_READY:
                    break
                if not _should_retry_login_with_next_proxy(result, attempt_index, proxy_attempts):
                    break

            if result is None or handler is None or settings is None:
                raise BotError("gcash flow did not start")
            self._send_result(chat_id, bot_input.email, job_id, result)

            if result.status == JobStatus.QR_READY:
                hold_seconds = await _setting_int(
                    settings,
                    "gcash_direct.browser_hold_seconds",
                    300,
                )
                if hold_seconds > 0:
                    self.telegram.send_message(
                        chat_id,
                        (
                            "QR da gui. Browser session se duoc giu song "
                            f"<code>{hold_seconds}s</code> de nhan callback sau scan."
                        ),
                    )
                    await self._watch_success(
                        chat_id=chat_id,
                        email=bot_input.email,
                        job=job,
                        handler=handler,
                        success_event=success_event,
                        timeout_seconds=min(
                            hold_seconds + 5,
                            self.config.success_timeout_seconds,
                        ),
                    )
        finally:
            if "job_id" in locals():
                self._success_events.pop(job_id, None)
            await engine.close()

    async def _probe_proxy_before_login(self, chat_id: str, proxy_url: str) -> bool:
        proxy_label = mask_proxy(proxy_url)
        ip_ok, ip_detail = await _probe_url(
            proxy_url,
            "https://api64.ipify.org",
            timeout=self.config.proxy_probe_timeout_seconds,
            impersonate=None,
        )
        chatgpt_ok, chatgpt_detail = await _probe_url(
            proxy_url,
            "https://chatgpt.com/auth/login",
            timeout=self.config.proxy_probe_timeout_seconds,
            impersonate=http.DEFAULT_IMPERSONATE,
        )
        message = (
            f"proxy_probe proxy={proxy_label} "
            f"ipify={ip_detail} chatgpt={chatgpt_detail}"
        )
        _log(message)
        self.telegram.send_message(chat_id, f"<code>{html.escape(message)}</code>")
        return ip_ok and chatgpt_ok

    async def _watch_success(
        self,
        *,
        chat_id: str,
        email: str,
        job: Job,
        handler: GcashDirectFlowHandler,
        success_event: threading.Event,
        timeout_seconds: int,
    ) -> None:
        deadline = time.monotonic() + max(1, timeout_seconds)
        interval = max(2, self.config.success_interval_seconds)
        last_plan = "unknown"
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            if success_event.is_set():
                return
            try:
                status = await handler.check_plan_status(job)
            except Exception as exc:  # noqa: BLE001 - watcher must not kill job.
                _log(f"success watch check failed: {type(exc).__name__}: {exc}")
                continue
            plan = str(status.get("plan") or "unknown").lower()
            last_plan = plan
            if plan == "plus":
                self.telegram.send_message(
                    chat_id,
                    (
                        "<b>GCash SUCCESS</b>\n"
                        f"acc: <b>{html.escape(_mask_email(email))}</b>\n"
                        "Plan da len: <code>plus</code>"
                    ),
                )
                return
        self.telegram.send_message(
            chat_id,
            (
                "Chua detect Plus sau khi doi scan.\n"
                f"acc: <b>{html.escape(_mask_email(email))}</b>\n"
                f"plan cuoi: <code>{html.escape(last_plan)}</code>"
            ),
        )

    def _send_result(
        self,
        chat_id: str,
        email: str,
        job_id: str,
        result: JobResult,
    ) -> None:
        if result.status != JobStatus.QR_READY:
            self.telegram.send_message(
                chat_id,
                (
                    "<b>GCash failed</b>\n"
                    f"job: <code>{html.escape(job_id)}</code>\n"
                    f"acc: <b>{html.escape(_mask_email(email))}</b>\n"
                    f"status: <code>{html.escape(result.status.value)}</code>\n"
                    f"code: <code>{html.escape(result.error_code or '')}</code>\n"
                    f"{html.escape((result.error_message or '')[:900])}"
                ),
            )
            return

        if result.artifact_path:
            path = Path(result.artifact_path)
            if path.is_file():
                png = path.read_bytes()
                if png.startswith(b"\x89PNG\r\n\x1a\n"):
                    self.telegram.send_photo(
                        chat_id,
                        _prepare_telegram_qr_png(png),
                        f"GCash QR - {email}",
                    )
                else:
                    self.telegram.send_message(chat_id, "QR artifact khong phai PNG hop le.")
            else:
                self.telegram.send_message(chat_id, "QR artifact path khong ton tai.")
        else:
            self.telegram.send_message(
                chat_id,
                "Da co payment link nhung chua lay duoc QR image tu browser.",
            )

        if result.payment_link:
            self._send_link(chat_id, result.payment_link)

        self.telegram.send_message(
            chat_id,
            (
                "<b>GCash ready</b>\n"
                f"job: <code>{html.escape(job_id)}</code>\n"
                f"acc: <b>{html.escape(_mask_email(email))}</b>\n"
                f"plan hien tai: <code>{html.escape(result.plan or 'unknown')}</code>\n"
                "QR ready khong dong nghia da Plus; can scan/authorize roi check plan."
            ),
        )

    def _send_link(self, chat_id: str, link: str) -> None:
        if len(link) <= 3500:
            self.telegram.send_plain(chat_id, "PAYMENT LINK\n" + link)
            return
        self.telegram.send_document(
            chat_id,
            "gcash_payment_link.txt",
            link.encode("utf-8"),
            "GCash payment link",
        )

    def _send_help(self, chat_id: str) -> None:
        self.telegram.send_message(
            chat_id,
            (
                "<b>GCash QR Bot</b>\n"
                "Gui account:\n"
                "<code>email|password|totp_secret|hostname:port:username:password</code>\n\n"
                "Cung ho tro proxy o dong 2 va cac format:\n"
                "<code>socks5://username:password@host:port\n"
                "username:password@hostname:port\n"
                "hostname:port@username:password</code>\n\n"
                "Bot nay chay GCash Direct flow rieng trong private chat, "
                "khong phai notifier group cua tool."
            ),
        )


class TelegramProgressHandler(logging.Handler):
    def __init__(
        self,
        chat_id: str,
        telegram: TelegramClient,
        email: str | None = None,
        success_event: threading.Event | None = None,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.chat_id = chat_id
        self.telegram = telegram
        self.email = email
        self.success_event = success_event
        self._last_sent_at = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            _log(message)
            if _is_gcash_success_log(message):
                if self.success_event is not None and not self.success_event.is_set():
                    self.success_event.set()
                    self.telegram.send_message(
                        self.chat_id,
                        (
                            "<b>GCash SUCCESS</b>\n"
                            f"acc: <b>{html.escape(_mask_email(self.email or ''))}</b>\n"
                            "Browser da ve trang success/plus_onboarding."
                        ),
                    )
            if not _should_forward_log(message):
                return
            now = time.monotonic()
            if now - self._last_sent_at < 1.5:
                return
            self._last_sent_at = now
            self.telegram.send_message(
                self.chat_id,
                f"<code>{html.escape(message[:900])}</code>",
            )
        except Exception:
            pass


def _make_job_logger_factory(
    chat_id: str,
    telegram: TelegramClient,
    email: str | None = None,
    success_event: threading.Event | None = None,
) -> Any:
    def factory(name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not any(isinstance(handler, TelegramProgressHandler) for handler in logger.handlers):
            handler = TelegramProgressHandler(
                chat_id,
                telegram,
                email,
                success_event,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
            logger.addHandler(handler)
        return logger

    return factory


def _prepare_telegram_qr_png(raw_png: bytes) -> bytes:
    """Wrap browser QR PNG in a scannable white square for Telegram preview."""
    try:
        from PIL import Image

        source = Image.open(BytesIO(raw_png)).convert("RGB")
        canvas_size = 470
        qr_size = 300
        source = source.resize((qr_size, qr_size), Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (canvas_size, canvas_size), "white")
        offset = (canvas_size - qr_size) // 2
        canvas.paste(source, (offset, offset))
        out = BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - raw QR is still better than no QR.
        _log(f"telegram qr frame failed: {type(exc).__name__}: {exc}")
        return raw_png


async def _setting_int(settings: Any, key: str, default: int) -> int:
    value = await settings.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _request(
    method: str,
    url: str,
    *,
    json_payload: Any | None = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> bytes:
    body = raw_body
    request_headers = dict(headers or {})
    if json_payload is not None:
        body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise TelegramHttpError(exc.code, exc.read()) from exc
    except urllib.error.URLError as exc:
        raise BotError(f"network error: {exc}") from exc


def _parse_bot_input(text: str) -> BotInput | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > 2:
        return None
    parts = [part.strip() for part in lines[0].split("|")]
    if len(parts) < 3:
        return None
    if "@" not in parts[0] or not parts[1] or not parts[2]:
        return None
    proxy_raw: str | None = None
    if len(parts) >= 4 and parts[3]:
        proxy_raw = "|".join(parts[3:]).strip()
    if len(lines) == 2:
        if proxy_raw:
            return None
        proxy_raw = lines[1]

    proxy_line: str | None = None
    if proxy_raw:
        try:
            proxy_line = _normalize_proxy_line(proxy_raw)
        except ValueError:
            return None
    return BotInput(
        account_line="|".join(parts[:3]),
        email=parts[0],
        proxy_line=proxy_line,
    )


def _normalize_proxy_line(raw: str) -> str:
    line = raw.strip()
    if not line:
        raise ValueError("empty proxy")

    # Provider format: host:port@username:password. Convert to the canonical
    # colon form that project proxy helpers already support.
    if "@" in line and "://" not in line:
        left, _, right = line.partition("@")
        left_parts = left.split(":")
        right_parts = right.split(":", 1)
        if (
            len(left_parts) == 2
            and left_parts[0]
            and left_parts[1].isdigit()
            and len(right_parts) == 2
            and right_parts[0]
        ):
            line = f"{left_parts[0]}:{left_parts[1]}:{right_parts[0]}:{right_parts[1]}"

    # Validate all supported forms early. The flow will materialize again when
    # picking the per-job proxy, so return the raw/canonical line.
    materialize_proxy(line)
    return line


def _proxy_attempts(proxy_line: str | None) -> list[str | None]:
    attempts: list[str | None] = [proxy_line]
    for variant in _socks_variants(proxy_line):
        if variant not in attempts:
            attempts.append(variant)
    return attempts


def _socks_variants(proxy_line: str | None) -> list[str]:
    if not proxy_line or "://" in proxy_line:
        return []
    try:
        url = materialize_proxy(proxy_line)
    except ValueError:
        return []
    if not url.startswith("http://"):
        return []
    suffix = url[len("http://") :]
    return [f"socks5://{suffix}", f"socks5h://{suffix}"]


async def _probe_url(
    proxy_url: str,
    endpoint: str,
    *,
    timeout: int,
    impersonate: str | None,
) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        async with http.create_async_client(
            proxy=proxy_url,
            timeout=float(max(3, timeout)),
            allow_redirects=True,
            impersonate=impersonate,
        ) as client:
            response = await client.get(endpoint)
        elapsed = time.monotonic() - started
        if 200 <= response.status_code < 400:
            return True, f"ok:{response.status_code}:{elapsed:.1f}s"
        return False, f"http_{response.status_code}:{elapsed:.1f}s"
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises.
        elapsed = time.monotonic() - started
        detail = str(exc).splitlines()[0][:140]
        return False, f"{type(exc).__name__}:{elapsed:.1f}s:{detail}"


def _should_retry_login_with_next_proxy(
    result: JobResult,
    attempt_index: int,
    attempts: list[str | None],
) -> bool:
    if attempt_index >= len(attempts):
        return False
    if result.status != JobStatus.ERROR:
        return False
    if result.error_code != "login_failed":
        return False
    message = (result.error_message or "").lower()
    return "network_error" in message or "timeout" in message


def _should_forward_log(message: str) -> bool:
    needles = (
        "flow start",
        "chatgpt_login begin",
        "chatgpt_login ok",
        "session_cache hit",
        "gcash proxy main",
        "chatgpt_checkout ok",
        "gcash_check",
        "gcash_confirm ok",
        "payment_link_ready",
        "payment_link_resolved",
        "gcash_browser session_status",
        "gcash_browser_qr ok",
        "gcash_browser_hold url",
        "flow error",
        "flow unexpected error",
    )
    return any(needle in message for needle in needles)


def _is_gcash_success_log(message: str) -> bool:
    if "gcash_browser_hold url=" not in message:
        return False
    return (
        "chatgpt.com/payments/success" in message
        or "refresh_account=true" in message
        or "plus_onboarding" in message
    )


def _mask_email(email: str) -> str:
    local, sep, domain = email.partition("@")
    if not sep:
        return email[:2] + "***"
    return f"{local[:1]}***@{domain}"


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    return [text[i : i + max_len] for i in range(0, len(text), max_len)]


def _short_text(body: bytes, limit: int = 500) -> str:
    text = body.decode("utf-8", errors="replace")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _join_and_cleanup(worker: threading.Thread, cleanup: Any) -> None:
    worker.join()
    cleanup()


def _log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def _normalize_bot_token(token: str) -> str:
    token = token.strip()
    if token.startswith("bot"):
        token = token[3:].strip()
    if re.match(r"^T\d+:", token):
        token = token[1:]
    if not re.match(r"^\d+:[A-Za-z0-9_-]+$", token):
        raise SystemExit(
            "Invalid TELEGRAM_BOT_TOKEN format. Expected '<digits>:<secret>'."
        )
    return token


def _load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
    return Config(
        bot_token=_normalize_bot_token(token),
        db_path=Path(
            os.environ.get("GCASH_PRIVATE_BOT_DB_PATH", str(DEFAULT_DB_PATH))
        ).resolve(),
        session_cache_dir=Path(
            os.environ.get(
                "GCASH_PRIVATE_BOT_SESSION_CACHE",
                str(DEFAULT_SESSION_CACHE_DIR),
            )
        ).resolve(),
        qr_dir=Path(os.environ.get("GCASH_PRIVATE_BOT_QR_DIR", str(DEFAULT_QR_DIR))).resolve(),
        delete_input=os.environ.get("GCASH_PRIVATE_BOT_DELETE_INPUT", "true")
        .strip()
        .lower()
        not in {"0", "false", "no", "off"},
        require_proxy=os.environ.get("GCASH_PRIVATE_BOT_REQUIRE_PROXY", "true")
        .strip()
        .lower()
        not in {"0", "false", "no", "off"},
        http_timeout_seconds=_env_int("GCASH_PRIVATE_BOT_HTTP_TIMEOUT", 75),
        success_timeout_seconds=_env_int("GCASH_PRIVATE_BOT_SUCCESS_TIMEOUT", 300),
        success_interval_seconds=_env_int("GCASH_PRIVATE_BOT_SUCCESS_INTERVAL", 8),
        proxy_probe=os.environ.get("GCASH_PRIVATE_BOT_PROXY_PROBE", "true")
        .strip()
        .lower()
        not in {"0", "false", "no", "off"},
        proxy_probe_timeout_seconds=_env_int("GCASH_PRIVATE_BOT_PROXY_PROBE_TIMEOUT", 20),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if sys.platform == "win32":
        _log(
            "windows event loop policy="
            + os.environ.get("GCASH_PRIVATE_BOT_EVENT_LOOP", "proactor")
            .strip()
            .lower()
        )
    bot = GcashPrivateTelegramBot(_load_config())
    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
