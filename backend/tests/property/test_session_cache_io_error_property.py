"""Property test cho `core/session_cache.py::AccountSessionCache.save` (Property 35).

**Property 35: Lỗi I/O khi ghi cache không bao giờ raise ra ngoài**
Dùng `hypothesis` + mock filesystem giả lập lỗi I/O trong `save`.

**Validates: Requirements 10.7**
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from app.core.session_cache import AccountSessionCache


class _StubSettingsRepository:
    """Stub `SettingsRepository` — `AccountSessionCache.__init__` chỉ giữ
    reference, không gọi method nào của repo trong luồng save() hiện tại."""


def _make_cache(cache_dir: Path) -> AccountSessionCache:
    cache = AccountSessionCache(
        settings=_StubSettingsRepository(),  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )
    cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24}
    )
    return cache


_io_error_strategy = st.sampled_from(
    [
        OSError("disk full"),
        PermissionError("denied"),
        OSError("no space left on device"),
    ]
)

_payload_strategy = st.dictionaries(
    keys=st.text(min_size=1, max_size=20),
    values=st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
    max_size=5,
)


@given(
    account_key=st.text(min_size=1, max_size=50),
    payload=_payload_strategy,
    io_error=_io_error_strategy,
)
@settings(max_examples=50)
def test_save_never_raises_on_io_error(
    account_key: str, payload: dict, io_error: Exception
) -> None:
    """`save()` bị inject lỗi I/O ngẫu nhiên (qua patch `Path.write_text`) —
    dù thất bại, KHÔNG exception nào lọt ra ngoài (R10.7), và `get()` sau
    đó trả `None` vì không có gì được ghi thành công."""

    async def _run() -> None:
        with TemporaryDirectory() as tmp_dir:
            cache = _make_cache(Path(tmp_dir))

            raised: Exception | None = None
            try:
                with patch.object(Path, "write_text", side_effect=io_error):
                    await cache.save(account_key, payload)
            except Exception as exc:  # noqa: BLE001 — test phải bắt MỌI exception lọt ra
                raised = exc

            assert raised is None, (
                f"save() đã raise exception ra ngoài khi gặp lỗi I/O "
                f"(vi phạm R10.7 Fail_Fast_Policy exception): {raised!r}"
            )

            result = await cache.get(account_key)
            assert result is None

    asyncio.run(_run())
