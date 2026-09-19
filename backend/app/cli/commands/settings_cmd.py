"""Sub-command `ideal-qr settings {get, set, dump}` — Settings_Store CRUD.

Requirement 15.2 (command spec), 11.3/11.4 (whitelist + type constraint validate).

3 sub-sub-command:
- `get [<key>] [--prefix <ns>]`: in giá trị. Không có key/prefix → dump toàn bộ.
- `set <key> <value>`: parse value theo TypeConstraint của key, gọi set(),
  bắt SettingsValidationError → in key/reason ra stderr, exit 1.
- `dump`: dump toàn bộ Settings ra stdout JSON (redact key nhạy cảm).
"""

from __future__ import annotations

import argparse
import json
import sys

from app.cli.bootstrap import bootstrap_cli
from app.cli.formatters import EventFormatter, JsonFormatter, TextFormatter
from app.core.errors import SettingsValidationError
from app.core.redaction import redact_dict


EXIT_OK = 0
EXIT_CONFIG_ERROR = 1


def _get_formatter(args: argparse.Namespace) -> EventFormatter:
    return TextFormatter() if args.format == "text" else JsonFormatter()


def _parse_value(value_str: str, constraint_type: str) -> object:
    """Parse chuỗi CLI thành đúng type theo TypeConstraint.type."""
    if constraint_type == "int":
        return int(value_str)
    if constraint_type == "number":
        return float(value_str)
    if constraint_type == "bool":
        low = value_str.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"bool phải là true/false, nhận: {value_str!r}")
    if constraint_type == "string":
        return value_str
    if constraint_type in {"list_str", "list_object"}:
        parsed = json.loads(value_str)
        if not isinstance(parsed, list):
            raise ValueError(
                f"{constraint_type} phải là JSON array, nhận: {type(parsed).__name__}"
            )
        return parsed
    raise ValueError(f"Unknown constraint type: {constraint_type}")


async def cmd_settings_get(args: argparse.Namespace) -> int:
    db_path_override: str | None = args.db_path
    formatter = _get_formatter(args)
    key: str | None = args.key
    prefix: str | None = args.prefix

    services = await bootstrap_cli(db_path_override)
    try:
        if key:
            value = await services.settings.get(key)
            if value is None:
                print(f"Key '{key}' chưa được set", file=sys.stderr, flush=True)
                return EXIT_CONFIG_ERROR
            print(formatter.format_settings_map({key: value}), flush=True)
        elif prefix:
            values = await services.settings.list(prefix=prefix)
            print(formatter.format_settings_map(values), flush=True)
        else:
            # Không key, không prefix → dump toàn bộ (redact secret)
            values = await services.settings.list()
            print(formatter.format_settings_map(redact_dict(values)), flush=True)
        return EXIT_OK
    finally:
        await services.db_engine.close()


async def cmd_settings_set(args: argparse.Namespace) -> int:
    db_path_override: str | None = args.db_path
    key: str = args.key
    value_str: str = args.value

    services = await bootstrap_cli(db_path_override)
    try:
        # Tra constraint để biết type parse
        constraint = services.settings.get_constraint(key)
        if constraint is None:
            print(
                f"Key '{key}' không thuộc whitelist Settings_Store",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_CONFIG_ERROR

        # Parse value theo type
        try:
            parsed_value = _parse_value(value_str, constraint.type)
        except (ValueError, json.JSONDecodeError) as ex:
            print(
                f"Không parse được value cho '{key}' (type={constraint.type}): {ex}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_CONFIG_ERROR

        # Ghi qua SettingsRepository.set — validate whitelist + range/enum
        try:
            await services.settings.set(key, parsed_value)
        except SettingsValidationError as ex:
            print(
                f"Settings validation error: key={ex.key} reason={ex.reason}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_CONFIG_ERROR

        print(f"OK: {key} = {json.dumps(parsed_value, default=str)}", flush=True)
        return EXIT_OK
    finally:
        await services.db_engine.close()


async def cmd_settings_dump(args: argparse.Namespace) -> int:
    db_path_override: str | None = args.db_path
    formatter = _get_formatter(args)

    services = await bootstrap_cli(db_path_override)
    try:
        values = await services.settings.list()
        # Redact secret trước khi in
        print(formatter.format_settings_map(redact_dict(values)), flush=True)
        return EXIT_OK
    finally:
        await services.db_engine.close()


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register `settings` sub-command (có 3 sub-sub-command)."""
    parser = subparsers.add_parser("settings", help="Thao tác Settings_Store")
    sub_sub = parser.add_subparsers(dest="settings_action", required=True)

    p_get = sub_sub.add_parser("get", help="Đọc 1 key hoặc namespace")
    p_get.add_argument(
        "key",
        nargs="?",
        default=None,
        help="Key cụ thể (bỏ trống → dùng --prefix hoặc dump toàn bộ)",
    )
    p_get.add_argument("--prefix", default=None, help="Filter theo namespace prefix")
    p_get.set_defaults(handler=cmd_settings_get)

    p_set = sub_sub.add_parser("set", help="Ghi 1 key (validate qua whitelist)")
    p_set.add_argument("key", help="Key format namespace.field")
    p_set.add_argument(
        "value",
        help="Value (int/number/bool parse literal, list_str/list_object parse JSON)",
    )
    p_set.set_defaults(handler=cmd_settings_set)

    p_dump = sub_sub.add_parser("dump", help="Dump toàn bộ Settings (redact secret)")
    p_dump.set_defaults(handler=cmd_settings_dump)
