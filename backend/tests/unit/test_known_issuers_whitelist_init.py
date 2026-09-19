"""Task 35.6: Unit test whitelist `ideal.known_issuers` khởi tạo tối thiểu 11 mã.

Requirement 6.4: `register_ideal_namespace()` PHẢI init `ideal.known_issuers`
chứa (ít nhất) 11 mã IssuerBank:
    ASNBNL21, RABONL2U, ABNANL2A, KNABNL2H, BITSNL2A, RBRBNL21,
    BUNQNL2A, TRIONL2U, FVLBNL22, NTSBDEB1, REVOLT21.

Chiến lược test:
    1. Dựng `SettingsRepository` trên DB SQLite tạm (fixture `sqlite_path`
       + `DbEngine.init_schema()`).
    2. Gọi `await register_ideal_namespace(settings)` để trigger seed
       default cho `ideal.known_issuers`.
    3. Đọc lại giá trị qua `settings.get("ideal.known_issuers")` — phải là
       `list[str]` non-None (constraint whitelist đã đăng ký + default đã
       seed vì key chưa từng được set).
    4. Assert 11 mã trên là subset của set(known_issuers) — cho phép danh
       sách default rộng hơn 11 mã trong tương lai (Requirement 6.4 dùng
       chữ "tối thiểu"), miễn không thiếu bất kỳ mã bắt buộc nào.

Requirements: 6.4.
"""

from __future__ import annotations

from pathlib import Path

from app.core.db import DbEngine
from app.core.settings_store import SettingsRepository
from app.payments.ideal import register_ideal_namespace


REQUIRED_ISSUER_CODES: frozenset[str] = frozenset(
    {
        "ASNBNL21",
        "RABONL2U",
        "ABNANL2A",
        "KNABNL2H",
        "BITSNL2A",
        "RBRBNL21",
        "BUNQNL2A",
        "TRIONL2U",
        "FVLBNL22",
        "NTSBDEB1",
        "REVOLT21",
    }
)


async def test_register_ideal_namespace_seeds_default_known_issuers_with_11_required_codes(
    sqlite_path: Path,
) -> None:
    """Sau `register_ideal_namespace()`, `ideal.known_issuers` chứa đủ 11 mã bắt buộc."""
    engine = DbEngine(sqlite_path)
    try:
        await engine.init_schema()
        settings = SettingsRepository(engine)

        await register_ideal_namespace(settings)

        default_known_issuers = await settings.get("ideal.known_issuers")

        assert default_known_issuers is not None, (
            "register_ideal_namespace() phải seed default `ideal.known_issuers` "
            "khi key chưa từng được set (Requirement 6.4)"
        )
        assert isinstance(default_known_issuers, list), (
            "Kiểu dữ liệu mong đợi list[str], nhận "
            f"{type(default_known_issuers).__name__}"
        )
        assert all(isinstance(item, str) for item in default_known_issuers), (
            "Mọi phần tử trong `ideal.known_issuers` phải là string (BIC code)"
        )

        known_issuers_set: set[str] = set(default_known_issuers)
        missing_codes = REQUIRED_ISSUER_CODES - known_issuers_set
        assert not missing_codes, (
            "Whitelist `ideal.known_issuers` thiếu các mã bắt buộc "
            f"(Requirement 6.4): {sorted(missing_codes)}"
        )
        assert len(known_issuers_set) >= 11, (
            "Whitelist phải có tối thiểu 11 mã BIC unique, thực tế "
            f"{len(known_issuers_set)}"
        )
    finally:
        await engine.close()


async def test_register_ideal_namespace_registers_known_issuers_constraint_as_list_str(
    sqlite_path: Path,
) -> None:
    """Sau `register_ideal_namespace()`, constraint `ideal.known_issuers` = list_str."""
    engine = DbEngine(sqlite_path)
    try:
        await engine.init_schema()
        settings = SettingsRepository(engine)

        await register_ideal_namespace(settings)

        constraint = settings.get_constraint("ideal.known_issuers")

        assert constraint is not None, (
            "register_ideal_namespace() phải đăng ký constraint cho "
            "`ideal.known_issuers` vào whitelist (Requirement 11.7)"
        )
        assert constraint.type == "list_str", (
            "Kiểu constraint mong đợi 'list_str' để chứa list mã BIC, "
            f"thực tế {constraint.type!r}"
        )
    finally:
        await engine.close()
