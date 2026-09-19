"""Settings Store — nguồn cấu hình duy nhất của toàn Backend_Service (SQLite).

Thuộc `core/` (Payment_Module_Boundary — Requirement 13.6): module này KHÔNG
được import bất kỳ gì từ `app.payments.*`. `SettingsRepository` chỉ tự đăng
ký sẵn các namespace CORE dùng chung (`ui.*`, `proxy.*`, `session_cache.*`).
Namespace `ideal.*` (đặc thù payment method) SHALL được `payments/ideal/__init__.py`
tự đăng ký sau qua `register_namespace("ideal", {...})` — module này KHÔNG
hardcode danh sách key `ideal.*`.

Requirements: 9.1, 10.1, 11.1, 11.2, 11.3, 11.4, 11.6, 11.7.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from app.core.db import DbEngine
from app.core.errors import NamespaceAlreadyRegisteredError, SettingsValidationError

_UPSERT_SETTING_SQL = """
INSERT INTO settings (key, value, updated_at)
VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
ON CONFLICT(key) DO UPDATE SET
    value = excluded.value,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now');
"""


@dataclass(frozen=True)
class TypeConstraint:
    """Ràng buộc kiểu dữ liệu + phạm vi hợp lệ cho 1 key thuộc whitelist.

    Dùng để validate generic — `SettingsRepository` không cần biết ý nghĩa
    nghiệp vụ của key, chỉ cần biết `TypeConstraint` tương ứng.

    Attributes:
        type: 1 trong 6 kiểu hỗ trợ. `min`/`max` áp dụng khác nhau theo kiểu:
            - "int"/"number": bound giá trị số trực tiếp.
            - "string": bound độ dài chuỗi (ví dụ `min=1` nghĩa là non-empty).
            - "list_str"/"list_object": bound độ dài (số phần tử) của list.
        min: Giới hạn dưới (bao gồm), `None` nghĩa là không giới hạn dưới.
        max: Giới hạn trên (bao gồm), `None` nghĩa là không giới hạn trên.
        enum: Danh sách giá trị hợp lệ (áp dụng cho kiểu "string"), `None`
            nghĩa là không giới hạn theo enum.
        item_schema: Chỉ dùng cho kiểu "list_object" — dict dạng
            `{"required_keys": [...]}` để validate mỗi object trong list có
            đủ các key bắt buộc (validate shape cơ bản, không phải full JSON
            schema).
    """

    type: Literal["int", "number", "bool", "string", "list_str", "list_object"]
    min: float | None = None
    max: float | None = None
    enum: list[str] | None = None
    item_schema: dict | None = None


class SettingsRepository:
    """Nguồn cấu hình runtime duy nhất — whitelist key + type constraint (R11).

    Mỗi key có format `namespace.field` (dot-separated, lowercase). Mỗi
    namespace được đăng ký (`register_namespace`) đúng 1 lần bởi module chịu
    trách nhiệm — `core/` tự đăng ký `ui`, `proxy`, `session_cache` khi khởi
    tạo; `payments/ideal/__init__.py` đăng ký thêm `ideal` sau, tại thời điểm
    import (side-effect có kiểm soát), để không vi phạm Payment_Module_Boundary.
    """

    def __init__(self, engine: DbEngine) -> None:
        self._engine = engine
        self._registry: dict[str, dict[str, TypeConstraint]] = {}

        self.register_namespace(
            "ui",
            {
                # Nội dung textarea "Nhập account" của Frontend_App. Lưu trực
                # tiếp string đa dòng (mỗi dòng 1 account) để mọi client
                # cùng thấy nội dung y hệt khi mở tool — hữu ích khi 1 nhóm
                # dev/ops share cùng backend qua tunnel. KHÔNG persist qua
                # localStorage phía FE (R11.5) — chỉ nguồn duy nhất SQLite.
                # Cap 100k ký tự = ~2000 dòng account để tránh nuốt DB.
                "input_draft": TypeConstraint(type="string", min=0, max=100_000),
                # Bật/tắt tính năng auto-reload "Check Plus All" từ FE:
                # cứ mỗi `auto_check_plus_all_interval_seconds` giây,
                # Frontend_App tự gọi bulk check-plan cho toàn bộ job
                # qr_ready đang có. Tắt (default) để giữ hành vi cũ —
                # user vẫn phải bấm nút Check Plus All thủ công.
                "auto_check_plus_all_enabled": TypeConstraint(type="bool"),
                # Chu kỳ auto-reload (giây) — chỉ có hiệu lực khi
                # `auto_check_plus_all_enabled=true`. Min 5s để tránh
                # user đặt quá thấp DoS session cache + ChatGPT endpoint.
                # Max 3600s (1h) đủ cho use-case "check định kỳ nhẹ".
                # Default 60s ở FE (fallback khi key chưa set) — cân
                # bằng giữa realtime perception và tải request.
                "auto_check_plus_all_interval_seconds": TypeConstraint(
                    type="int", min=5, max=3600
                ),
            },
        )
        self.register_namespace(
            "proxy",
            {
                "list": TypeConstraint(type="list_str"),
                "rotation_mode": TypeConstraint(
                    type="string", enum=["round_robin", "least_used"]
                ),
                "max_leases_per_proxy": TypeConstraint(type="int", min=1),
                "dead_threshold": TypeConstraint(type="int", min=1),
                # TTL (giây) proxy nằm trong "quarantine dead" trước khi tự
                # revive về alive. `0` = tắt TTL (dead vĩnh viễn, hành vi
                # cũ trước 2026-07). Trần 3600s = 1h để tránh user set nhầm
                # con số lớn khiến pool tê liệt lâu quá; đủ headroom vẫn.
                "dead_cooldown_seconds": TypeConstraint(type="int", min=0, max=3600),
                # ---- Preflight probe knobs (2026-07, port từ gpt_signup_hybrid) ----
                # Bật preflight probe TRƯỚC khi cấp lease cho job. Fail auth
                # (host không resolve / 407 proxy-auth) → force_dead line
                # ngay (không đợi threshold). Fail ip (timeout/reset) →
                # rotate SID nếu line có `{SID}`, hoặc thử proxy khác.
                "probe_enabled": TypeConstraint(type="bool"),
                # Endpoint dùng để probe. KHÔNG dùng chatgpt.com vì có thể
                # bị fingerprint-gate / rate-limit. api64.ipify.org trả IP
                # thô, không rate limit, phù hợp làm probe target.
                "probe_endpoint": TypeConstraint(type="string", min=8, max=200),
                "probe_timeout_seconds": TypeConstraint(type="int", min=3, max=30),
                # Số proxy tối đa 1 job sẽ thử trước khi bỏ cuộc (→ direct
                # nếu `fallback_direct_on_exhausted` bật, hoặc `proxy_exhausted`
                # error). Default 10 phù hợp pool ~10 proxy có template SID,
                # user set qua UI khi cần siết lại để fail-fast.
                "probe_max_tries": TypeConstraint(type="int", min=1, max=20),
                # Với template line có `{SID}`, số lần rotate SID cho CÙNG
                # 1 line khi probe fail reason=ip. 0 = không rotate (fail
                # 1 lần là next line).
                "probe_sid_retry_per_line": TypeConstraint(type="int", min=0, max=10),
                # Semaphore process-global cho probe. Giới hạn N probe
                # song song để tránh burst khi Multi-N job start cùng lúc.
                # Trần 100 để hỗ trợ pool lớn (~100 proxy) trong "Test all"
                # + preflight — probe song song đi qua NHIỀU proxy khác nhau,
                # không dồn về 1 IP nên không DoS provider endpoint. User
                # muốn siết lại thì set thấp qua UI.
                "probe_concurrency": TypeConstraint(type="int", min=1, max=100),
                # Khi tất cả proxy fail probe (hoặc `proxy_exhausted`),
                # có auto-chạy Direct_Mode không. Default `true` để job
                # không chết oan khi pool tạm cạn — user opt-out qua UI
                # nếu ưu tiên bảo vệ IP thật hơn tỉ lệ hoàn thành job.
                "fallback_direct_on_exhausted": TypeConstraint(type="bool"),
            },
        )
        self.register_namespace(
            "session_cache",
            {
                "enabled": TypeConstraint(type="bool"),
                "ttl_hours": TypeConstraint(type="int", min=1, max=720),
                # Ngưỡng "cache còn trẻ": nếu tuổi cache < N giờ, `_resolve_session`
                # SKIP `revalidate` HTTP call, chỉ hydrate cookies từ cache vào
                # jar và dùng luôn. Tiết kiệm 1 round-trip qua Cloudflare —
                # đặc biệt hữu ích với rotating proxy, khi mỗi request phải
                # đi qua CF challenge trên IP mới. `0` = disable (luôn revalidate,
                # backward-compat). Cap `<=` ttl_hours để không lớn hơn TTL.
                "skip_revalidate_if_fresh_hours": TypeConstraint(
                    type="int", min=0, max=720
                ),
            },
        )

    def register_namespace(self, namespace: str, key_constraints: dict[str, TypeConstraint]) -> None:
        """Đăng ký 1 namespace mới cùng type constraint của các field thuộc nó.

        Raise `NamespaceAlreadyRegisteredError` nếu namespace đã được đăng ký
        trước đó, tránh 2 module tranh nhau đăng ký cùng 1 namespace
        (Requirement 13.6).
        """
        if namespace in self._registry:
            raise NamespaceAlreadyRegisteredError(namespace)
        self._registry[namespace] = dict(key_constraints)

    def _resolve_constraint(self, key: str) -> TypeConstraint:
        """Trả về `TypeConstraint` của `key` nếu thuộc whitelist.

        Raise `SettingsValidationError` nếu key sai format (không phải
        `namespace.field`) hoặc không thuộc bất kỳ namespace/field đã đăng ký
        (Requirement 11.4) — áp dụng cho cả đọc VÀ ghi.
        """
        namespace, _, field = key.partition(".")
        if not namespace or not field:
            raise SettingsValidationError(
                key, "invalid key, must follow format 'namespace.field'"
            )

        namespace_constraints = self._registry.get(namespace)
        if namespace_constraints is None or field not in namespace_constraints:
            raise SettingsValidationError(key, "key is not in the whitelist")

        return namespace_constraints[field]

    def get_constraint(self, key: str) -> TypeConstraint | None:
        """Tra `TypeConstraint` của `key` cho các tầng UI/CLI cần biết type parse.

        Đây là helper thuần đọc (không chạm state, không I/O, không raise) —
        dùng khi tầng ngoài (ví dụ `settings_cmd.py` CLI) cần biết type của
        1 key để parse giá trị người dùng nhập thành đúng kiểu Python trước
        khi gọi `set()`/`bulk_set()`.

        Không expose bản thân internal registry `_registry` — chỉ trả object
        `TypeConstraint` tương ứng với 1 key cụ thể, giữ nguyên encapsulation.

        Args:
            key: Key format `namespace.field` (dot-separated, lowercase).

        Returns:
            `TypeConstraint` nếu `key` thuộc whitelist đã đăng ký; `None`
            nếu key sai format (không có dấu `.`) hoặc namespace/field chưa
            được `register_namespace`. Caller tự quyết định cách xử lý
            `None` (ví dụ CLI in lỗi "unknown key" và exit).
        """
        namespace, _, field = key.partition(".")
        if not namespace or not field:
            return None
        return self._registry.get(namespace, {}).get(field)

    async def get(self, key: str) -> Any | None:
        """Đọc giá trị đã set của `key`, `None` nếu key chưa từng được set.

        Raise `SettingsValidationError` nếu `key` không thuộc whitelist
        (Requirement 11.4).
        """
        self._resolve_constraint(key)

        connection = await self._engine.get_connection()
        async with connection.execute(
            "SELECT value FROM settings WHERE key = ?;", (key,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None
        return json.loads(row[0])

    async def set(self, key: str, value: Any) -> None:
        """Ghi `value` cho `key` sau khi validate whitelist + type constraint.

        Raise `SettingsValidationError` (giữ nguyên giá trị cũ, KHÔNG ghi đè)
        nếu `key` không thuộc whitelist hoặc `value` không khớp
        `TypeConstraint` (Requirement 11.3, 11.4).
        """
        constraint = self._resolve_constraint(key)
        self._validate_type_constraint(key, value, constraint)

        connection = await self._engine.get_connection()
        await connection.execute(_UPSERT_SETTING_SQL, (key, json.dumps(value)))
        await connection.commit()

    async def bulk_get(self, keys: list[str]) -> dict[str, Any]:
        """Đọc nhiều key 1 lần. Key chưa từng set trả `None` trong kết quả.

        Raise `SettingsValidationError` nếu bất kỳ key nào không thuộc
        whitelist (validate toàn bộ trước khi đọc, Fail_Fast_Policy).
        """
        for key in keys:
            self._resolve_constraint(key)

        result: dict[str, Any] = {key: None for key in keys}
        if not keys:
            return result

        connection = await self._engine.get_connection()
        placeholders = ",".join("?" for _ in keys)
        query = f"SELECT key, value FROM settings WHERE key IN ({placeholders});"
        async with connection.execute(query, keys) as cursor:
            rows = await cursor.fetchall()

        for row_key, row_value in rows:
            result[row_key] = json.loads(row_value)
        return result

    async def bulk_set(self, items: dict[str, Any]) -> None:
        """Ghi nhiều key-value 1 lần, atomic (Fail_Fast_Policy).

        Validate TOÀN BỘ `items` trước khi ghi bất kỳ item nào — nếu 1 item
        vi phạm whitelist/type constraint, raise `SettingsValidationError`
        ngay và KHÔNG ghi item nào. Nếu ghi vào DB thất bại giữa chừng (lỗi
        I/O...), rollback toàn bộ transaction.
        """
        encoded_items: list[tuple[str, str]] = []
        for key, value in items.items():
            constraint = self._resolve_constraint(key)
            self._validate_type_constraint(key, value, constraint)
            encoded_items.append((key, json.dumps(value)))

        connection = await self._engine.get_connection()
        try:
            for key, encoded_value in encoded_items:
                await connection.execute(_UPSERT_SETTING_SQL, (key, encoded_value))
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        """Trả toàn bộ key-value đã set trong DB, filter theo `prefix` (namespace) nếu có."""
        connection = await self._engine.get_connection()
        if prefix is not None:
            async with connection.execute(
                "SELECT key, value FROM settings WHERE key LIKE ?;", (f"{prefix}.%",)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with connection.execute("SELECT key, value FROM settings;") as cursor:
                rows = await cursor.fetchall()

        return {row_key: json.loads(row_value) for row_key, row_value in rows}

    def _validate_type_constraint(self, key: str, value: Any, constraint: TypeConstraint) -> None:
        """Validate `value` khớp `constraint`, raise `SettingsValidationError` với `reason` cụ thể."""
        expected_type = constraint.type

        if expected_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingsValidationError(
                    key, f"expected type int, got {type(value).__name__}"
                )
            self._check_numeric_range(key, value, constraint)

        elif expected_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SettingsValidationError(
                    key, f"expected type number, got {type(value).__name__}"
                )
            self._check_numeric_range(key, value, constraint)

        elif expected_type == "bool":
            if not isinstance(value, bool):
                raise SettingsValidationError(
                    key, f"expected type bool, got {type(value).__name__}"
                )

        elif expected_type == "string":
            if not isinstance(value, str):
                raise SettingsValidationError(
                    key, f"expected type string, got {type(value).__name__}"
                )
            self._check_length_range(key, len(value), constraint)
            self._check_enum(key, value, constraint)

        elif expected_type == "list_str":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise SettingsValidationError(
                    key, f"expected type list-of-string, got {type(value).__name__}"
                )
            self._check_length_range(key, len(value), constraint)

        elif expected_type == "list_object":
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise SettingsValidationError(
                    key, f"expected type list-of-object, got {type(value).__name__}"
                )
            self._check_length_range(key, len(value), constraint)
            self._check_item_schema(key, value, constraint)

        else:  # pragma: no cover — Literal type giới hạn 6 giá trị hợp lệ ở trên
            raise SettingsValidationError(key, f"unsupported type constraint: {expected_type}")

    @staticmethod
    def _range_desc(constraint: TypeConstraint) -> str:
        lower = constraint.min if constraint.min is not None else "-∞"
        upper = constraint.max if constraint.max is not None else "+∞"
        return f"[{lower}, {upper}]"

    def _check_numeric_range(self, key: str, value: float, constraint: TypeConstraint) -> None:
        if constraint.min is not None and value < constraint.min:
            raise SettingsValidationError(
                key, f"value {value} is outside range {self._range_desc(constraint)}"
            )
        if constraint.max is not None and value > constraint.max:
            raise SettingsValidationError(
                key, f"value {value} is outside range {self._range_desc(constraint)}"
            )

    def _check_length_range(self, key: str, length: int, constraint: TypeConstraint) -> None:
        if constraint.min is not None and length < constraint.min:
            raise SettingsValidationError(
                key, f"length {length} is outside range {self._range_desc(constraint)}"
            )
        if constraint.max is not None and length > constraint.max:
            raise SettingsValidationError(
                key, f"length {length} is outside range {self._range_desc(constraint)}"
            )

    @staticmethod
    def _check_enum(key: str, value: str, constraint: TypeConstraint) -> None:
        if constraint.enum is not None and value not in constraint.enum:
            raise SettingsValidationError(
                key, f"value '{value}' is not in enum {constraint.enum}"
            )

    @staticmethod
    def _check_item_schema(key: str, items: list[dict], constraint: TypeConstraint) -> None:
        if constraint.item_schema is None:
            return
        required_keys: list[str] = constraint.item_schema.get("required_keys", [])
        for index, item in enumerate(items):
            missing = [required_key for required_key in required_keys if required_key not in item]
            if missing:
                raise SettingsValidationError(
                    key, f"object at index {index} is missing required field(s): {missing}"
                )
