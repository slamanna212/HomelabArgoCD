#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("ai_usage_collector", pathlib.Path(__file__).with_name("ai_usage_collector.py"))
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(collector)


class CollectorParsingTests(unittest.TestCase):
    def test_claude_flat_and_dynamic_limits(self):
        result = collector.parse_claude_usage(
            {
                "five_hour": {"utilization": 42.5, "resets_at": "2026-08-24T12:00:00Z"},
                "seven_day": {"utilization": 68, "resets_at": "2026-08-25T12:00:00+00:00"},
                "extra_usage": {
                    "is_enabled": True,
                    "utilization": 23,
                    "used_credits": 12345,
                    "monthly_limit": 50000,
                },
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": 91,
                        "resets_at": "2026-08-26T00:00:00Z",
                        "is_active": True,
                        "scope": {"model": {"display_name": "Claude Sonnet"}, "surface": "api"},
                    }
                ],
            }
        )
        windows = {item["window"]: item for item in result["windows"]}
        self.assertEqual(windows["session"]["used_percent"], 42.5)
        self.assertEqual(windows["weekly"]["remaining_percent"], 32)
        self.assertEqual(windows["claude_weekly_scoped_claude_sonnet_api"]["used_percent"], 91)
        self.assertEqual(result["extra"]["used"], 123.45)
        self.assertEqual(result["extra"]["limit"], 500.0)

    def test_claude_new_spend_shape(self):
        result = collector.parse_claude_usage(
            {
                "spend": {
                    "enabled": True,
                    "percent": 12.5,
                    "used": {"amount_minor": 1250, "currency": "USD", "exponent": 2},
                    "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2},
                }
            }
        )
        self.assertEqual(result["extra"], {"enabled": True, "used": 12.5, "limit": 100.0, "percent": 12.5, "unit": "usd"})

    def test_codex_usage_and_additional_limit(self):
        result = collector.parse_codex_usage(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {"used_percent": 15, "reset_at": 1700000000},
                    "secondary_window": {"used_percent": 5, "reset_at": 1701000000},
                },
                "credits": {"has_credits": True, "unlimited": False, "balance": 150},
                "additional_rate_limits": [
                    {
                        "limit_name": "Codex Spark",
                        "rate_limit": {"primary_window": {"used_percent": 80, "reset_at": 1700000000}},
                    }
                ],
            }
        )
        windows = {item["window"]: item for item in result["windows"]}
        self.assertEqual(windows["session"]["remaining_percent"], 85)
        self.assertEqual(windows["codex_spark_session"]["used_percent"], 80)
        self.assertEqual(result["credits"]["remaining"], 150)
        self.assertEqual(result["plan"], "pro")

    def test_api_balance_shapes(self):
        self.assertEqual(
            collector.parse_openrouter_credits({"data": {"total_credits": 100, "total_usage": 12.5}})["credits"]["remaining"],
            87.5,
        )
        self.assertEqual(
            collector.parse_deepseek_balance({"balance_infos": [{"currency": "USD", "total_balance": 4.25}]})["credits"]["remaining"],
            4.25,
        )


class OAuthStateTests(unittest.TestCase):
    def test_pkce_pending_state_and_atomic_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "claude" / "oauth.json"
            pending_path = pathlib.Path(directory) / "claude" / "pending.json"
            with patch.dict(os.environ, {"CLAUDE_STATE": str(state_path), "CLAUDE_PENDING_STATE": str(pending_path)}, clear=False):
                url = collector.create_claude_auth_url()
                self.assertTrue(url.startswith(collector.CLAUDE_AUTHORIZE_URL + "?"))
                pending = json.loads(pending_path.read_text())
                self.assertEqual(len(pending["state"]), 43)
                self.assertTrue(pending["verifier"])
                collector._atomic_write_json(state_path, {"access_token": "redacted-test", "refresh_token": "refresh-test"})
                mode = stat.S_IMODE(state_path.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_exchange_rotates_refresh_token_and_removes_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "oauth.json"
            pending_path = pathlib.Path(directory) / "pending.json"
            with patch.dict(os.environ, {"CLAUDE_STATE": str(state_path), "CLAUDE_PENDING_STATE": str(pending_path)}, clear=False):
                collector._atomic_write_json(pending_path, {"verifier": "verifier", "state": "state", "expires_at": 9999999999})
                with patch.object(
                    collector,
                    "_request_json",
                    side_effect=[
                        {"access_token": "access-test", "refresh_token": "refresh-rotated", "expires_in": 3600},
                        {"account": {"email": "sam@example.invalid", "uuid": "account-id"}},
                    ],
                ):
                    result = collector.exchange_claude_code("code#state")
                self.assertTrue(result["saved"])
                saved = json.loads(state_path.read_text())
                self.assertEqual(saved["refresh_token"], "refresh-rotated")
                self.assertFalse(pending_path.exists())


if __name__ == "__main__":
    unittest.main()
