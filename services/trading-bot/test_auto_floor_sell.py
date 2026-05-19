import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("auto_floor_sell.py")
SPEC = importlib.util.spec_from_file_location("auto_floor_sell", MODULE_PATH)
auto_floor_sell = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = auto_floor_sell
SPEC.loader.exec_module(auto_floor_sell)


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeBroker:
    def is_market_open(self, now_kst):
        return False


class KisTokenReuseTests(unittest.TestCase):
    def _params(self, **kwargs):
        params = {
            "api_key": "key",
            "api_secret": "secret",
            "cano": "12345678",
            "acnt_prdt_cd": "01",
            "env": "real",
        }
        params.update(kwargs)
        return params

    def test_reuses_cached_token_when_within_window(self):
        issued_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
        provider = auto_floor_sell.KisProvider(
            self._params(
                access_token="cached-token",
                access_token_issued_at=issued_at.isoformat().replace("+00:00", "Z"),
                token_reuse_hours=21,
            )
        )

        with patch.object(provider, "_issue_token", side_effect=AssertionError("must not issue token")):
            headers = provider._auth_header()

        self.assertEqual(headers["authorization"], "Bearer cached-token")
        self.assertIsNone(provider.issued_new_token_state())

    def test_issues_new_token_when_cached_timestamp_invalid(self):
        with patch.object(
            auto_floor_sell.requests,
            "post",
            return_value=_FakeResponse({"access_token": "new-token"}),
        ) as mocked_post:
            provider = auto_floor_sell.KisProvider(
                self._params(access_token="cached-token", access_token_issued_at="not-a-timestamp")
            )
            headers = provider._auth_header()

        self.assertEqual(headers["authorization"], "Bearer new-token")
        self.assertEqual(mocked_post.call_count, 1)
        token_state = provider.issued_new_token_state()
        self.assertIsNotNone(token_state)
        self.assertEqual(token_state["access_token"], "new-token")
        self.assertTrue(token_state["access_token_issued_at"].endswith("Z"))

    def test_writes_token_state_file_only_for_new_token(self):
        with patch.object(
            auto_floor_sell.requests,
            "post",
            return_value=_FakeResponse({"access_token": "refreshed-token"}),
        ):
            provider = auto_floor_sell.KisProvider(self._params())
            provider._auth_header()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "token-state.json"
            auto_floor_sell.write_token_state_output(provider, str(output_path))
            self.assertTrue(output_path.exists())

            data = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(data["access_token"], "refreshed-token")
            self.assertTrue(data["access_token_issued_at"].endswith("Z"))

    def test_run_auto_floor_sell_applies_cli_token_overrides(self):
        captured_config = {}

        def fake_build_provider(account_config):
            captured_config.update(account_config)
            return _FakeBroker()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "account.json"
            config_path.write_text(
                json.dumps({"provider": "kis", "params": {"env": "real"}}),
                encoding="utf-8",
            )

            with patch.object(auto_floor_sell, "build_provider", side_effect=fake_build_provider):
                result = auto_floor_sell.run_auto_floor_sell(
                    config_path=str(config_path),
                    sell_ratio=0.10,
                    lookback_days=365,
                    dry_run=False,
                    read_only=False,
                    order_mode="best_limit",
                    limit_offset_bps=20,
                    access_token="cli-token",
                    access_token_issued_at="2026-05-19T00:05:12Z",
                    token_reuse_hours=21,
                )

        self.assertEqual(result, 0)
        params = captured_config["params"]
        self.assertEqual(params["access_token"], "cli-token")
        self.assertEqual(params["access_token_issued_at"], "2026-05-19T00:05:12Z")
        self.assertEqual(params["token_reuse_hours"], 21)


if __name__ == "__main__":
    unittest.main()
