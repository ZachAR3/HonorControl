"""Tests for CLI safety: no backend imports, deterministic exit codes (WP-16)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from honor_control.cli.honorctl import (
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_OPERATION_FAILED,
    EXIT_PROTOCOL,
    EXIT_SERVICE_UNAVAILABLE,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    build_parser,
)

CLI_PATH = Path(__file__).parent.parent / "honor_control" / "cli" / "honorctl.py"


class TestCliSafety:
    """Verify the CLI never imports backend implementation classes."""

    def test_no_backend_implementation_imports(self) -> None:
        """The CLI must not import from honor_control.backend."""
        source = CLI_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not node.module or not node.module.startswith(
                    "honor_control.backend"
                ), f"CLI imports backend module: {node.module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("honor_control.backend"), (
                        f"CLI imports backend: {alias.name}"
                    )

    def test_no_realbackend_reference(self) -> None:
        """The CLI must not reference RealBackend."""
        source = CLI_PATH.read_text(encoding="utf-8")
        assert "RealBackend" not in source

    def test_no_sys_exit_in_handlers(self) -> None:
        """Command handlers must not call sys.exit() internally."""
        source = CLI_PATH.read_text(encoding="utf-8")
        # sys.exit is only called in main() for the top-level return.
        # Check that no cmd_ function body contains sys.exit.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("cmd_"):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = child.func
                        if isinstance(func, ast.Attribute) and func.attr == "exit":
                            assert False, f"sys.exit found in {node.name}"

    def test_exit_codes_are_distinct(self) -> None:
        codes = {
            EXIT_OK,
            EXIT_USAGE,
            EXIT_UNAVAILABLE,
            EXIT_OPERATION_FAILED,
            EXIT_NOT_AUTHORIZED,
            EXIT_SERVICE_UNAVAILABLE,
            EXIT_PROTOCOL,
        }
        assert len(codes) == 7
        assert EXIT_OK == 0

    def test_parser_has_bus_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--bus", "session", "status"])
        assert args.bus == "session"

    def test_parser_has_timeout_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--timeout", "5", "status"])
        assert args.timeout == 5.0

    def test_parser_has_json_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--json", "status"])
        assert args.json is True

    def test_battery_thresholds_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["battery", "thresholds", "80", "75"])
        assert args.end == 80
        assert args.start == 75

    def test_battery_mode_rejects_custom(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["battery", "mode", "custom"])

    def test_fan_manual_has_ttl(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["fan", "manual", "50", "--ttl", "120"])
        assert args.speed == 50
        assert args.ttl == 120

    def test_power_profile_save_has_complete_variables(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "power",
                "save",
                "compile",
                "--pl1",
                "30",
                "--pl2",
                "45",
                "--epp",
                "balance_performance",
                "--turbo",
                "off",
                "--max-perf",
                "85",
            ]
        )
        assert args.name == "compile"
        assert args.pl1 == 30
        assert args.turbo == "off"
        assert args.max_perf == 85

    def test_auto_switch_accepts_profile_and_script_options(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "power",
                "auto-switch",
                "on",
                "--on-ac",
                "performance",
                "--on-battery",
                "silent",
                "--ac-script",
                "/usr/local/bin/ac-hook",
            ]
        )
        assert args.on_ac == "performance"
        assert args.on_battery == "silent"
        assert args.ac_script == "/usr/local/bin/ac-hook"
