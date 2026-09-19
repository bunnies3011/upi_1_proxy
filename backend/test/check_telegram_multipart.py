"""Smoke check TelegramBotClient.send_photo dùng CurlMime đúng.

KHÔNG gọi Telegram thật — chỉ verify:
    1. `curl_cffi.CurlMime` import OK.
    2. `TelegramBotClient.send_photo` build được CurlMime + gọi curl_cffi
       KHÔNG raise `NotImplementedError` (mock server).

Cách test: dùng `unittest.mock` patch `client.post` để trả FakeResponse,
verify request đi qua đúng đường `multipart=` chứ không phải `files=`.
"""
from __future__ import annotations

import asyncio
import inspect
import sys


def _pass(name: str, detail: str = "") -> None:
    print(f"[PASS] {name} :: {detail}", flush=True)


def _fail(name: str, detail: str) -> None:
    print(f"[FAIL] {name} :: {detail}", flush=True)
    sys.exit(1)


async def _run() -> None:
    # (1) CurlMime import.
    try:
        from curl_cffi import CurlMime
        m = CurlMime()
        m.addpart(name="a", data=b"hello")
    except Exception as exc:
        _fail("CurlMime import + addpart", f"{type(exc).__name__}: {exc}")
    _pass("CurlMime import + addpart", "OK")

    # (2) TelegramBotClient.send_photo signature dùng multipart=.
    from app.notifiers.telegram.client import TelegramBotClient

    src = inspect.getsource(TelegramBotClient.send_photo)
    if "multipart=mime" not in src:
        _fail("send_photo uses multipart=", "source missing 'multipart=mime'")
    # Chỉ fail nếu `files=` xuất hiện trong CALL SITE (dòng có `.post(` gần
    # đó), không phải trong comment/docstring.
    code_lines = [
        line for line in src.splitlines()
        if not line.lstrip().startswith("#") and not line.lstrip().startswith('"')
    ]
    code_only = "\n".join(code_lines)
    if "files=" in code_only:
        _fail("send_photo no files=", "code still has 'files=' outside comments")
    _pass("send_photo API migrated", "multipart=mime, no files= in code")

    # (3) Mock post → verify call kwargs.
    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"ok":true,"result":{}}'

        def json(self):
            return {"ok": True, "result": {}}

    class _FakeClient:
        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeResp()

    bot = TelegramBotClient()
    bot._sending_client = _FakeClient()  # type: ignore[assignment]
    await bot.send_photo(
        token="TEST_TOKEN",
        chat_id="123",
        photo_bytes=b"\x89PNG\r\n\x1a\n",
        caption="hi",
    )
    kwargs = captured.get("kwargs") or {}
    if "multipart" not in kwargs:
        _fail("post kwargs contain multipart", f"got {list(kwargs.keys())}")
    if "files" in kwargs:
        _fail("post kwargs no files", "files= still passed")
    if "data" in kwargs:
        _fail("post kwargs no data", "data= should not co-exist with multipart")
    _pass("post kwargs shape", f"multipart present, no files/data — url={captured['url']}")

    print("\n[SMOKE] telegram multipart migration OK ✓", flush=True)


if __name__ == "__main__":
    asyncio.run(_run())
