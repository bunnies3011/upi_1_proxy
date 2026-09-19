"""Fixture chung cho toàn bộ test suite (unit/property/integration).

Chưa import module cụ thể của app (core/payments chưa tồn tại ở bước scaffold
này) — các fixture liên quan tới DB/app sẽ được mở rộng dần khi các module đó
được implement.

Bug fix (dedupe/verify session): nhiều test dùng `DbEngine(...)` nhưng KHÔNG
`await engine.close()` → aiosqlite spawn background thread không exit khi
process kết thúc → pytest process HANG sau khi test đã PASS. Fix: autouse
fixture theo dõi mọi `DbEngine` được khởi tạo trong 1 test qua `WeakSet`,
sau khi test kết thúc thì `await engine.close()` với mọi instance còn kết
nối mở. Monkey-patch `DbEngine.__init__` 1 lần duy nhất ở module scope
(không phá production runtime vì chỉ chạy trong pytest process).
"""

from __future__ import annotations

import sqlite3
import weakref
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from hypothesis import settings as hypothesis_settings

# pytest-asyncio mode "auto" được cấu hình ở backend/pytest.ini
# (asyncio_mode = auto) — mọi test `async def` chạy trực tiếp, không cần
# đánh dấu @pytest.mark.asyncio thủ công.

# Giảm max_examples mặc định của toàn bộ property test (hypothesis) để chạy
# nhanh hơn trong vòng lặp dev/CI — 20 example là đủ để phát hiện phần lớn
# edge case cho các property đơn giản trong project này, thay vì mặc định
# 100. Test nào cần nhiều example hơn cho 1 property phức tạp cụ thể có thể
# override bằng @settings(max_examples=...) riêng ở file đó.
hypothesis_settings.register_profile("fast", max_examples=20)
hypothesis_settings.load_profile("fast")


# ---------------------------------------------------------------------------
# DbEngine tracking — root-cause fix cho pytest hang sau khi test PASS.
#
# aiosqlite.Connection giữ 1 background thread per connection. Nếu test tạo
# `DbEngine(...)` nhưng không `await engine.close()`, thread đó không exit,
# pytest process kẹt ở finalize.
#
# Autouse async fixture bên dưới sẽ:
#   1) Trước test: snapshot WeakSet hiện tại (không strong-ref các instance
#      cũ để không leak giữa test).
#   2) Sau test: iterate _live_engines, gọi `await engine.close()` cho
#      instance nào còn `_connection is not None`. `close()` là idempotent
#      theo doc-string của DbEngine → an toàn khi test đã tự close.
# ---------------------------------------------------------------------------


def _install_db_engine_tracker() -> list:
    """Monkey-patch `DbEngine.__init__` một lần duy nhất để append mỗi
    instance mới vào 1 list global (STRONG REF). Trả về list để autouse
    fixture dùng.

    Vì sao STRONG REF (không phải WeakSet)? Test function kết thúc thì
    locals (repo, engine) bị dereferenced và GC có thể chạy TRƯỚC khi
    autouse fixture chạy phần `yield` return path — khi đó WeakSet đã
    trống, không còn instance để close. Strong ref giữ engine sống đến
    khi autouse fixture close explicit; sau đó `del` xoá strong ref để
    GC dọn bình thường.

    Import trong hàm để không require `app.core.db` ở module-import time
    của conftest (an toàn cho stage scaffold khi module chưa tồn tại).
    """
    from app.core.db import DbEngine  # noqa: PLC0415 — defer import

    registry: list = []  # strong refs tới mọi DbEngine tạo trong test

    # Guard idempotent: nếu đã patch (test suite reload), giữ nguyên.
    if getattr(DbEngine.__init__, "_conftest_tracked", False):
        return getattr(DbEngine, "_conftest_engine_registry")  # type: ignore[attr-defined]

    original_init = DbEngine.__init__

    def tracked_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_init(self, *args, **kwargs)
        registry.append(self)

    tracked_init._conftest_tracked = True  # type: ignore[attr-defined]
    DbEngine.__init__ = tracked_init  # type: ignore[method-assign]
    DbEngine._conftest_engine_registry = registry  # type: ignore[attr-defined]
    return registry


_ENGINE_REGISTRY = _install_db_engine_tracker()


@pytest.fixture(autouse=True)
async def _close_leaked_db_engines() -> AsyncIterator[None]:
    """Sau mỗi test, đóng mọi `DbEngine` khởi tạo TRONG test đó nhưng
    chưa được caller close — tránh aiosqlite thread leak khiến pytest hang.

    Chỉ close instance TẠO SAU khi fixture bắt đầu (dùng snapshot index),
    tránh close nhầm engine của test khác. Idempotent: nếu test đã tự
    `await engine.close()`, `engine._connection is None` → no-op.

    Sau khi close, `del _ENGINE_REGISTRY[start_idx:]` xoá strong ref để
    GC dọn bình thường — không leak memory qua test files.
    """
    start_idx = len(_ENGINE_REGISTRY)
    yield
    to_close = _ENGINE_REGISTRY[start_idx:]
    for engine in to_close:
        try:
            if engine._connection is not None:  # noqa: SLF001 — cleanup hook
                await engine.close()
        except Exception as exc:  # noqa: BLE001 — cleanup không được silent
            import sys  # noqa: PLC0415

            print(
                f"[conftest cleanup] close engine {engine!r} raised: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
    # Drop strong refs sau close để không giữ engine sống ngoài scope test.
    del _ENGINE_REGISTRY[start_idx:]


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    """Đường dẫn file SQLite tạm, riêng biệt cho mỗi test."""
    return tmp_path / "ideal_qr_tool_test.db"


@pytest.fixture
def sqlite_connection(sqlite_path: Path) -> Iterator[sqlite3.Connection]:
    """Kết nối SQLite đồng bộ trên file tạm, tự đóng sau mỗi test.

    Dùng cho test cần một DB SQLite thật (không mock) nhưng chưa cần tới
    `DbEngine`/`aiosqlite` (sẽ được implement ở task core/db.py).
    """
    connection = sqlite3.connect(sqlite_path)
    try:
        yield connection
    finally:
        connection.close()
