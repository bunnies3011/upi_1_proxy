"""Unit test cho `core/session_cache.py::AccountSessionCache` (task 7.1).

Chỉ verify các case cụ thể (round-trip trong TTL, hết TTL, disabled, lỗi
I/O không raise, clear idempotent, clear_all, tách file theo hash, filename
không chứa raw account_key). Property test tổng quát (Property 32-37)
thuộc các task riêng 7.2-7.7 ở `tests/property/`.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import pytest

from app.core.session_cache import AccountSessionCache, CachedSession


class _StubSettingsRepository:
    """Stub `SettingsRepository` — `AccountSessionCache.__init__` chỉ giữ
    reference, không gọi method nào của repo trong luồng hiện tại (hydrate
    đi qua `apply_settings(snapshot)` truyền từ test)."""


def _make_cache(
    cache_dir: Path,
    *,
    enabled: bool = True,
    ttl_hours: int = 24,
) -> AccountSessionCache:
    cache = AccountSessionCache(
        settings=_StubSettingsRepository(),  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )
    cache.apply_settings(
        {"session_cache.enabled": enabled, "session_cache.ttl_hours": ttl_hours}
    )
    return cache


def _current_epoch() -> float:
    """Lấy epoch giây hiện tại — dùng thay `time.time()` trong assert để
    tránh phụ thuộc monkeypatch của test khác."""
    import time as _time

    return _time.time()


# ---------------------------------------------------------------------------
# Test 1: save + get round-trip, payload đúng
# ---------------------------------------------------------------------------


async def test_save_then_get_round_trip_returns_original_payload(
    tmp_path: Path,
) -> None:
    """save() rồi get() trong TTL → trả đúng payload đã lưu (R10.2, R10.5)."""
    cache = _make_cache(tmp_path, ttl_hours=24)
    payload = {"cookie": "abc123", "access_token": "xyz", "nested": {"a": 1}}

    before = _current_epoch()
    await cache.save("account-1", payload)
    result = await cache.get("account-1")
    after = _current_epoch()

    assert result is not None
    assert isinstance(result, CachedSession)
    assert result.account_key == "account-1"
    assert result.payload == payload
    assert before <= result.saved_at <= after


# ---------------------------------------------------------------------------
# Test 2: TTL hết hạn → get trả None
# ---------------------------------------------------------------------------


async def test_get_returns_none_after_ttl_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get() sau khi vượt ttl_hours → None và xoá file expired (R10.3)."""
    cache = _make_cache(tmp_path, ttl_hours=1)
    fake_now = [1_000_000.0]
    monkeypatch.setattr("app.core.session_cache.time.time", lambda: fake_now[0])

    await cache.save("account-1", {"cookie": "abc"})
    # sanity — có đúng 1 file .json (hash-based name)
    files_before = list(tmp_path.glob("*.json"))
    assert len(files_before) == 1

    fake_now[0] += 3600.0 + 1.0  # vượt TTL 1 giờ đúng 1 giây

    assert await cache.get("account-1") is None
    assert list(tmp_path.glob("*.json")) == []  # self-heal xoá file expired


# ---------------------------------------------------------------------------
# Test 3: enabled=false → save no-op, get luôn None
# ---------------------------------------------------------------------------


async def test_disabled_save_is_noop_and_get_always_none(tmp_path: Path) -> None:
    """session_cache.enabled=false → save no-op, get luôn None (R10.6)."""
    cache = _make_cache(tmp_path, enabled=False)

    await cache.save("account-1", {"cookie": "abc"})

    # save() no-op → không có file nào được tạo
    assert list(tmp_path.glob("*.json")) == []

    # get() luôn None kể cả sau save
    assert await cache.get("account-1") is None


async def test_disabled_get_returns_none_even_when_file_exists_from_before(
    tmp_path: Path,
) -> None:
    """File cũ vẫn còn trên đĩa, nhưng khi enabled=false → get() phải None."""
    # Bước 1: ghi cache ở enabled=true
    cache = _make_cache(tmp_path, enabled=True)
    await cache.save("account-1", {"cookie": "abc"})
    assert len(list(tmp_path.glob("*.json"))) == 1

    # Bước 2: chuyển sang disabled — get() KHÔNG được đọc file cũ (R10.6)
    cache.apply_settings(
        {"session_cache.enabled": False, "session_cache.ttl_hours": 24}
    )
    assert await cache.get("account-1") is None


# ---------------------------------------------------------------------------
# Test 4: lỗi I/O trong save → không raise, log warning (R10.7)
# ---------------------------------------------------------------------------


async def test_save_io_error_does_not_raise_only_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Lỗi I/O khi ghi file cache → chỉ log warning, KHÔNG raise (R10.7 —
    ngoại lệ DUY NHẤT của Fail_Fast_Policy)."""
    cache = _make_cache(tmp_path)

    def _raise_oserror(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated: disk full")

    # Mock write_text để save() raise, xác nhận lỗi bị nuốt (chỉ log warning)
    monkeypatch.setattr(Path, "write_text", _raise_oserror)

    with caplog.at_level(logging.WARNING, logger="app.core.session_cache"):
        await cache.save("account-1", {"cookie": "abc"})  # KHÔNG raise

    assert any("write_error" in record.message for record in caplog.records)
    # Không có file final nào được tạo (write fail trước khi rename)
    assert list(tmp_path.glob("*.json")) == []
    # Cũng không để lại file .tmp rác
    assert list(tmp_path.glob("*.tmp")) == []


async def test_save_warning_does_not_leak_raw_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sensitive_Data_Redaction: log warning KHÔNG chứa payload thô
    (cookie/access_token) khi save() gặp lỗi I/O."""
    cache = _make_cache(tmp_path)

    def _raise_oserror(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated: disk full")

    monkeypatch.setattr(Path, "write_text", _raise_oserror)

    secret_cookie = "super_secret_cookie_value_deadbeef"
    secret_token = "sensitive_access_token_cafebabe"

    with caplog.at_level(logging.WARNING, logger="app.core.session_cache"):
        await cache.save(
            "account-1", {"cookie": secret_cookie, "access_token": secret_token}
        )

    all_logged = " ".join(record.getMessage() for record in caplog.records)
    assert secret_cookie not in all_logged
    assert secret_token not in all_logged


# ---------------------------------------------------------------------------
# Test 5: clear idempotent
# ---------------------------------------------------------------------------


async def test_clear_is_idempotent_on_nonexistent_key(tmp_path: Path) -> None:
    """clear() 2 lần liên tiếp trên account chưa từng save → không raise (R10.8)."""
    cache = _make_cache(tmp_path)

    await cache.clear("account-not-exist")  # lần 1
    await cache.clear("account-not-exist")  # lần 2 — idempotent


async def test_clear_removes_existing_cache_file(tmp_path: Path) -> None:
    """clear() xoá đúng file của account đó, không đụng file của account khác."""
    cache = _make_cache(tmp_path)
    await cache.save("account-1", {"cookie": "a"})
    await cache.save("account-2", {"cookie": "b"})
    assert len(list(tmp_path.glob("*.json"))) == 2

    await cache.clear("account-1")

    assert await cache.get("account-1") is None
    # account-2 vẫn còn nguyên
    result = await cache.get("account-2")
    assert result is not None
    assert result.payload == {"cookie": "b"}


# ---------------------------------------------------------------------------
# Test 6: clear_all xoá hết
# ---------------------------------------------------------------------------


async def test_clear_all_removes_every_json_file(tmp_path: Path) -> None:
    """clear_all() xoá toàn bộ .json file của mọi account (R10.9)."""
    cache = _make_cache(tmp_path)
    await cache.save("account-1", {"cookie": "a"})
    await cache.save("account-2", {"cookie": "b"})
    await cache.save("account-3", {"cookie": "c"})

    await cache.clear_all()

    assert await cache.get("account-1") is None
    assert await cache.get("account-2") is None
    assert await cache.get("account-3") is None
    assert list(tmp_path.glob("*.json")) == []


# ---------------------------------------------------------------------------
# Test 7: account_key khác → filename (sha256 hash) khác
# ---------------------------------------------------------------------------


async def test_different_account_keys_map_to_different_files(tmp_path: Path) -> None:
    """2 `account_key` khác nhau → 2 file khác nhau (sha256 hash khác)."""
    cache = _make_cache(tmp_path)
    await cache.save("account-1", {"cookie": "a"})
    await cache.save("account-2", {"cookie": "b"})

    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(files) == 2, f"Kỳ vọng 2 file riêng biệt, thấy {files}"

    # 2 filename phải khác nhau (không collision)
    assert files[0] != files[1]

    # Cross-check: verify get đúng payload cho từng key (không lẫn lộn)
    res1 = await cache.get("account-1")
    res2 = await cache.get("account-2")
    assert res1 is not None and res1.payload == {"cookie": "a"}
    assert res2 is not None and res2.payload == {"cookie": "b"}


async def test_same_account_key_overwrites_same_file(tmp_path: Path) -> None:
    """Cùng account_key gọi save() 2 lần → chỉ 1 file (ghi đè)."""
    cache = _make_cache(tmp_path)
    await cache.save("account-1", {"cookie": "old"})
    await cache.save("account-1", {"cookie": "new"})

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1

    result = await cache.get("account-1")
    assert result is not None
    assert result.payload == {"cookie": "new"}


# ---------------------------------------------------------------------------
# Test 8: filename KHÔNG chứa raw account_key
# ---------------------------------------------------------------------------


async def test_filename_does_not_contain_raw_account_key(tmp_path: Path) -> None:
    """Filename dùng sha256 hash — KHÔNG chứa `account_key` thô để không
    lộ định danh qua tên file trên đĩa (defensive redaction ở tầng core/).
    """
    cache = _make_cache(tmp_path)

    # Dùng account_key có ký tự rõ ràng (giả lập PII như email) để test
    # dễ nhận diện nếu bị leak thẳng vào tên file.
    raw_keys = [
        "user@example.com",
        "alice.raw.identifier",
        "unicode-tên-tài-khoản",
        "with-dashes_and_underscores",
    ]
    for k in raw_keys:
        await cache.save(k, {"cookie": "x"})

    filenames = [p.name for p in tmp_path.glob("*.json")]
    assert len(filenames) == len(raw_keys)

    for raw_key in raw_keys:
        for fname in filenames:
            assert raw_key not in fname, (
                f"Filename '{fname}' chứa raw account_key '{raw_key}' — leak PII!"
            )

    # Verify: tên file = sha256(raw_key)[:32] + ".json"
    for raw_key in raw_keys:
        expected_prefix = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32]
        expected_name = f"{expected_prefix}.json"
        assert expected_name in filenames, (
            f"Kỳ vọng filename '{expected_name}' cho key '{raw_key}'"
        )


# ---------------------------------------------------------------------------
# Extra: init tạo cache_dir, apply_settings default whitelist
# ---------------------------------------------------------------------------


def test_init_creates_cache_dir_if_missing(tmp_path: Path) -> None:
    """__init__ SHALL tạo cache_dir nếu chưa tồn tại (không hardcode path)."""
    cache_dir = tmp_path / "session_cache_nested" / "level2"
    assert not cache_dir.exists()

    AccountSessionCache(
        settings=_StubSettingsRepository(),  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )

    assert cache_dir.is_dir()


async def test_apply_settings_uses_whitelist_defaults_when_snapshot_missing(
    tmp_path: Path,
) -> None:
    """Key thiếu trong snapshot → dùng default whitelist (enabled=True,
    ttl_hours=24) theo Requirement 11.7."""
    cache = AccountSessionCache(
        settings=_StubSettingsRepository(),  # type: ignore[arg-type]
        cache_dir=tmp_path,
    )
    cache.apply_settings({})  # snapshot rỗng

    await cache.save("account-1", {"cookie": "abc"})
    result = await cache.get("account-1")

    # enabled default True → save+get hoạt động, không phải no-op
    assert result is not None
    assert result.payload == {"cookie": "abc"}


async def test_get_self_heals_corrupt_json_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """File JSON hỏng (không parse được) → get() trả None, xoá file,
    log warning để tự phục hồi (self-heal)."""
    cache = _make_cache(tmp_path)

    # Ghi thẳng file rác với đúng tên file mà cache sẽ đọc.
    filename = hashlib.sha256("account-1".encode("utf-8")).hexdigest()[:32] + ".json"
    corrupt_file = tmp_path / filename
    corrupt_file.write_text("not valid json {{{", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.core.session_cache"):
        result = await cache.get("account-1")

    assert result is None
    assert not corrupt_file.exists()  # đã self-heal
    assert any("corrupt" in record.message for record in caplog.records)
