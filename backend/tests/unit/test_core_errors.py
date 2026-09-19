"""Unit test cho `app.core.errors` — hierarchy exception chung của `core/`.

Kiểm tra:
- `ProxyExhaustedError` và `SettingsValidationError` đều là subclass của
  `CoreError`, nhưng KHÔNG là subclass của nhau (hierarchy song song, không
  chồng chéo).
- `CoreError` không kế thừa bất kỳ class nào ngoài `Exception` (đảm bảo
  Payment_Module_Boundary — không lệ thuộc `IdealFlowError`).
- Constructor truyền đúng attribute và message mô tả rõ nguyên nhân
  (Fail_Fast_Policy).

_Requirements: 9.4, 11.3, 11.4_
"""

from __future__ import annotations

from app.core.errors import CoreError, ProxyExhaustedError, SettingsValidationError


class TestHierarchy:
    def test_core_error_is_exception_only(self) -> None:
        assert CoreError.__bases__ == (Exception,)

    def test_proxy_exhausted_error_is_core_error(self) -> None:
        assert issubclass(ProxyExhaustedError, CoreError)

    def test_settings_validation_error_is_core_error(self) -> None:
        assert issubclass(SettingsValidationError, CoreError)

    def test_proxy_exhausted_error_is_not_settings_validation_error(self) -> None:
        assert not issubclass(ProxyExhaustedError, SettingsValidationError)

    def test_settings_validation_error_is_not_proxy_exhausted_error(self) -> None:
        assert not issubclass(SettingsValidationError, ProxyExhaustedError)


class TestProxyExhaustedError:
    def test_attributes_are_stored(self) -> None:
        error = ProxyExhaustedError(total_proxies=5, dead_count=3, leased_out_count=2)

        assert error.total_proxies == 5
        assert error.dead_count == 3
        assert error.leased_out_count == 2

    def test_message_contains_diagnostic_counts(self) -> None:
        error = ProxyExhaustedError(total_proxies=5, dead_count=3, leased_out_count=2)

        message = str(error)
        assert "total_proxies=5" in message
        assert "dead_count=3" in message
        assert "leased_out_count=2" in message

    def test_is_raisable_and_catchable_as_core_error(self) -> None:
        try:
            raise ProxyExhaustedError(total_proxies=1, dead_count=1, leased_out_count=0)
        except CoreError as caught:
            assert isinstance(caught, ProxyExhaustedError)
        else:
            raise AssertionError("ProxyExhaustedError phải được raise")


class TestSettingsValidationError:
    def test_attributes_are_stored(self) -> None:
        error = SettingsValidationError(
            key="ideal.max_concurrent",
            reason="giá trị vượt range [1, 50]",
        )

        assert error.key == "ideal.max_concurrent"
        assert error.reason == "giá trị vượt range [1, 50]"

    def test_message_contains_key_and_reason(self) -> None:
        error = SettingsValidationError(
            key="ideal.default_issuer",
            reason="không thuộc whitelist ideal.known_issuers",
        )

        message = str(error)
        assert "ideal.default_issuer" in message
        assert "không thuộc whitelist ideal.known_issuers" in message

    def test_is_raisable_and_catchable_as_core_error(self) -> None:
        try:
            raise SettingsValidationError(key="proxy.list", reason="kiểu dữ liệu mong đợi list-of-string")
        except CoreError as caught:
            assert isinstance(caught, SettingsValidationError)
        else:
            raise AssertionError("SettingsValidationError phải được raise")
