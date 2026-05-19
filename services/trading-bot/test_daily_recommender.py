import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("daily_recommender.py")
SPEC = importlib.util.spec_from_file_location("daily_recommender", MODULE_PATH)
daily_recommender = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = daily_recommender
SPEC.loader.exec_module(daily_recommender)


def make_bar(d, o, h, l, c, v):
    return daily_recommender.DailyBar(
        business_date=d, open=o, high=h, low=l, close=c, volume=v
    )


def make_flow(d, foreign, institution):
    return daily_recommender.InvestorFlowBar(
        business_date=d, foreign_net=foreign, institution_net=institution
    )


class IndicatorTests(unittest.TestCase):
    def test_count_up_days(self):
        bars = [
            make_bar(date(2026, 5, 1), 100, 101, 99, 99, 1000),  # down
            make_bar(date(2026, 5, 2), 100, 101, 99, 101, 1000),  # up
            make_bar(date(2026, 5, 3), 100, 102, 99, 102, 1000),  # up
            make_bar(date(2026, 5, 4), 100, 100, 99, 100, 1000),  # equal -> not up
            make_bar(date(2026, 5, 5), 100, 103, 99, 103, 1000),  # up
        ]
        self.assertEqual(daily_recommender.count_up_days(bars, 5), 3)
        self.assertEqual(daily_recommender.count_up_days(bars, 2), 1)

    def test_pullback_then_rebound(self):
        bars = [
            make_bar(date(2026, 5, 1), 0, 0, 0, 110, 0),
            make_bar(date(2026, 5, 2), 0, 0, 0, 108, 0),
            make_bar(date(2026, 5, 3), 0, 0, 0, 105, 0),
            make_bar(date(2026, 5, 4), 0, 0, 0, 100, 0),  # end of pullback (3 days)
            make_bar(date(2026, 5, 5), 0, 0, 0, 102, 0),  # rebound day 1
            make_bar(date(2026, 5, 6), 0, 0, 0, 104, 0),  # rebound day 2
        ]
        self.assertTrue(daily_recommender.has_pullback_then_rebound(bars, 3, 2))

    def test_pullback_then_rebound_failure_no_dip(self):
        bars = [
            make_bar(date(2026, 5, 1), 0, 0, 0, 100, 0),
            make_bar(date(2026, 5, 2), 0, 0, 0, 102, 0),
            make_bar(date(2026, 5, 3), 0, 0, 0, 103, 0),
            make_bar(date(2026, 5, 4), 0, 0, 0, 104, 0),
            make_bar(date(2026, 5, 5), 0, 0, 0, 105, 0),
            make_bar(date(2026, 5, 6), 0, 0, 0, 106, 0),
        ]
        self.assertFalse(daily_recommender.has_pullback_then_rebound(bars, 3, 2))

    def test_volume_surge_ratio(self):
        bars = [make_bar(date(2026, 5, i + 1), 0, 0, 0, 100, 100) for i in range(20)]
        bars.append(make_bar(date(2026, 5, 21), 0, 0, 0, 100, 300))
        self.assertAlmostEqual(daily_recommender.latest_volume_surge_ratio(bars, 20), 3.0)

    def test_rsi_extreme_up(self):
        closes = [100 + i for i in range(20)]
        self.assertEqual(daily_recommender.compute_rsi(closes, 14), 100.0)

    def test_rsi_mixed(self):
        closes = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108, 107]
        rsi = daily_recommender.compute_rsi(closes, 14)
        self.assertGreater(rsi, 50)
        self.assertLess(rsi, 100)

    def test_golden_cross_status_aligned(self):
        # rising series ensures short MA > mid MA > long MA
        closes = [100 + i for i in range(70)]
        qualified, disparity = daily_recommender.golden_cross_status(closes, 5, 20, 60)
        self.assertTrue(qualified)
        self.assertGreater(disparity, 0)

    def test_golden_cross_status_falling(self):
        closes = [200 - i for i in range(70)]
        qualified, _ = daily_recommender.golden_cross_status(closes, 5, 20, 60)
        self.assertFalse(qualified)


class FakeProvider:
    def __init__(self, *, universe, snapshot, ratio, profit, bars, flows, kospi=None):
        self._universe = universe
        self._snapshot = snapshot
        self._ratio = ratio
        self._profit = profit
        self._bars = bars
        self._flows = flows
        self._kospi = kospi or []

    def get_kospi_volume_universe(self, top_n):
        return list(self._universe)[:top_n]

    def get_price_snapshot(self, symbol):
        return dict(self._snapshot.get(symbol, {"current_price": 0, "per": 0, "pbr": 0, "eps": 0}))

    def get_financial_ratio(self, symbol):
        return dict(self._ratio.get(symbol, {"roe": 0.0}))

    def get_profit_ratio(self, symbol):
        return dict(self._profit.get(symbol, {"operating_profit": 0.0}))

    def get_daily_bars(self, symbol, days):
        return list(self._bars.get(symbol, []))

    def get_investor_flow(self, symbol, days):
        return list(self._flows.get(symbol, []))

    def get_kospi_daily_closes(self, days):
        return list(self._kospi)


def _build_uptrend_bars(symbol_base_price=100, length=70, surge=True):
    bars = []
    today = date(2026, 5, 19)
    # Pre-history: 65 days of slow uptrend, low volume.
    for i in range(length - 5):
        day = today - timedelta(days=length - i)
        price = symbol_base_price + i
        bars.append(make_bar(day, price, price + 0.5, price - 0.5, price + 0.5, 100))
    # Pullback 3 days
    last_close = bars[-1].close
    for j in range(3):
        day = today - timedelta(days=5 - j)
        c = last_close - (j + 1)
        bars.append(make_bar(day, last_close, last_close, c, c, 100))
    # Rebound 2 days, last with volume surge + big up bar
    pullback_close = bars[-1].close
    bars.append(make_bar(today - timedelta(days=2), pullback_close,
                         pullback_close + 2, pullback_close, pullback_close + 1, 110))
    bars.append(make_bar(today - timedelta(days=1), pullback_close + 1,
                         pullback_close + 4, pullback_close + 1, pullback_close + 3,
                         300 if surge else 80))
    bars.sort(key=lambda b: b.business_date)
    return bars


class EvaluateCandidateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = daily_recommender.RecommenderConfig(
            phase=3,
            top_n=5,
            max_per=100.0,
            min_roe=8.0,
            lookback_n=5,
            up_days_m=2,  # relax so synthetic bars qualify
            pullback_x=3,
            rebound_y=2,
            volume_window=20,
            volume_surge_ratio=2.0,
            ma_short=5,
            ma_mid=20,
            ma_long=60,
            max_disparity_pct=100.0,  # relax for synthetic uptrend
            invflow_days_lookback=5,
            invflow_days_k=3,
            rsi_period=14,
            rsi_max=99.5,
            kospi_ma_window=20,
            defensive_max_recommend=5,
            max_recommend=10,
        )
        self.item = daily_recommender.UniverseItem(
            symbol="005930", name="삼성전자", current_price=70000, trade_value=1e9
        )

    def _provider_for(self, bars, **overrides):
        kospi = [100 + i for i in range(30)]
        bars_map = {"005930": bars}
        snapshot = {"005930": {"current_price": 70000, "per": 12.0, "pbr": 1.2, "eps": 5000}}
        ratio = {"005930": {"roe": 12.0}}
        profit = {"005930": {"operating_profit": 1_000_000_000.0}}
        flows = {"005930": [make_flow(date(2026, 5, 19) - timedelta(days=i),
                                      1000, 500) for i in range(5)]}
        kwargs = dict(
            universe=[self.item], snapshot=snapshot, ratio=ratio,
            profit=profit, bars=bars_map, flows=flows, kospi=kospi,
        )
        kwargs.update(overrides)
        return FakeProvider(**kwargs)

    def test_recommendation_qualifies(self):
        bars = _build_uptrend_bars()
        provider = self._provider_for(bars)
        macro = daily_recommender.build_macro_context(provider, self.cfg)
        rec = daily_recommender.evaluate_candidate(provider, self.item, self.cfg, macro)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.symbol, "005930")
        self.assertGreater(rec.momentum_score, 0)
        self.assertTrue(any("양봉" in r for r in rec.reasons))

    def test_rejects_operating_loss(self):
        bars = _build_uptrend_bars()
        provider = self._provider_for(
            bars,
            profit={"005930": {"operating_profit": -100_000.0}},
        )
        macro = daily_recommender.build_macro_context(provider, self.cfg)
        rec = daily_recommender.evaluate_candidate(provider, self.item, self.cfg, macro)
        self.assertIsNone(rec)

    def test_rejects_high_per(self):
        bars = _build_uptrend_bars()
        provider = self._provider_for(
            bars,
            snapshot={"005930": {"current_price": 70000, "per": 200.0, "pbr": 1.2, "eps": 5000}},
        )
        macro = daily_recommender.build_macro_context(provider, self.cfg)
        rec = daily_recommender.evaluate_candidate(provider, self.item, self.cfg, macro)
        self.assertIsNone(rec)

    def test_rejects_no_volume_surge(self):
        bars = _build_uptrend_bars(surge=False)
        provider = self._provider_for(bars)
        macro = daily_recommender.build_macro_context(provider, self.cfg)
        rec = daily_recommender.evaluate_candidate(provider, self.item, self.cfg, macro)
        self.assertIsNone(rec)

    def test_rejects_overbought_rsi(self):
        bars = _build_uptrend_bars()
        cfg = self.cfg
        cfg.rsi_max = 1.0  # any RSI fails
        provider = self._provider_for(bars)
        macro = daily_recommender.build_macro_context(provider, self.cfg)
        rec = daily_recommender.evaluate_candidate(provider, self.item, cfg, macro)
        self.assertIsNone(rec)

    def test_defensive_max_recommend_in_downtrend(self):
        cfg = self.cfg
        cfg.defensive_max_recommend = 1
        cfg.max_recommend = 5
        provider = FakeProvider(
            universe=[],
            snapshot={}, ratio={}, profit={}, bars={}, flows={},
            kospi=[200 - i for i in range(30)],  # strictly falling
        )
        macro = daily_recommender.build_macro_context(provider, cfg)
        self.assertTrue(macro["available"])
        self.assertFalse(macro["uptrend"])
        self.assertEqual(macro["max_recommend"], 1)


class FormattingTests(unittest.TestCase):
    def test_markdown_table_with_results(self):
        recs = [
            daily_recommender.Recommendation(
                symbol="005930", name="삼성전자", current_price=70000.0,
                momentum_score=4.0, reasons=["5일 중 4일 양봉", "이평선 정배열"],
            )
        ]
        macro = {"available": True, "uptrend": True, "kospi_close": 2500.0,
                 "kospi_ma": 2400.0, "max_recommend": 5}
        out = daily_recommender.format_markdown_table(recs, macro)
        self.assertIn("| # | Symbol", out)
        self.assertIn("`005930`", out)
        self.assertIn("삼성전자", out)
        self.assertIn("5일 중 4일 양봉; 이평선 정배열", out)
        self.assertIn("Uptrend", out)

    def test_markdown_table_empty(self):
        out = daily_recommender.format_markdown_table([], {"available": False})
        self.assertIn("No stocks matched", out)


class CliTokenTests(unittest.TestCase):
    def test_run_applies_cli_token_overrides(self):
        captured = {}

        class _DummyProvider:
            def __init__(self):
                pass

            def issued_new_token_state(self):
                return None

        def fake_build_provider(account_config):
            captured.update(account_config)
            return _DummyProvider()

        def fake_recommend(provider, cfg):
            return [], {"available": False, "max_recommend": cfg.max_recommend}

        cfg = daily_recommender.RecommenderConfig(phase=1, top_n=5)

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "account.json"
            config_path.write_text(
                json.dumps({"provider": "kis", "params": {"env": "real"}}),
                encoding="utf-8",
            )
            with patch.object(daily_recommender, "build_provider", side_effect=fake_build_provider), \
                 patch.object(daily_recommender, "recommend", side_effect=fake_recommend):
                result = daily_recommender.run_daily_recommender(
                    config_path=str(config_path),
                    cfg=cfg,
                    api_call_delay=0.0,
                    recommend_json_output=None,
                    access_token="cli-token",
                    access_token_issued_at="2026-05-19T00:05:12Z",
                    token_reuse_hours=21,
                )

        self.assertEqual(result, 0)
        params = captured["params"]
        self.assertEqual(params["access_token"], "cli-token")
        self.assertEqual(params["access_token_issued_at"], "2026-05-19T00:05:12Z")
        self.assertEqual(params["token_reuse_hours"], 21)

    def test_main_validates_ma_ordering(self):
        with self.assertRaises(SystemExit):
            daily_recommender.main([
                "--config", "/tmp/does-not-exist.json",
                "--ma-short", "20", "--ma-mid", "5", "--ma-long", "60",
            ])


if __name__ == "__main__":
    unittest.main()
