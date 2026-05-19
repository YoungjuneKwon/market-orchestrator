#!/usr/bin/env python3
"""KIS API based daily stock recommender (KOSPI).

This script performs a multi-stage filtering pipeline to recommend KOSPI stocks
that have recently entered a positive momentum regime. It is designed to be run
as a daily batch job (for example through a GitHub Actions schedule).

It reuses the KisProvider authentication / token-reuse logic from
``auto_floor_sell`` so the same cached access token (Secret / Variable in CI)
can be shared across daily jobs.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests


# ---------------------------------------------------------------------------
# Reuse auto_floor_sell module (KisProvider, token cache logic, helpers).
# ---------------------------------------------------------------------------
_MODULE_PATH = Path(__file__).with_name("auto_floor_sell.py")
_SPEC = importlib.util.spec_from_file_location("auto_floor_sell", _MODULE_PATH)
if not _SPEC or not _SPEC.loader:  # pragma: no cover - defensive
    raise RuntimeError("Unable to load auto_floor_sell module")
auto_floor_sell = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault(_SPEC.name, auto_floor_sell)
_SPEC.loader.exec_module(auto_floor_sell)

KisProvider = auto_floor_sell.KisProvider
BrokerError = auto_floor_sell.BrokerError
ConfigError = auto_floor_sell.ConfigError
KST = auto_floor_sell.KST
_safe_float = auto_floor_sell._safe_float
_safe_int = auto_floor_sell._safe_int
load_account_config = auto_floor_sell.load_account_config
write_token_state_output = auto_floor_sell.write_token_state_output


# KRX trading-day density: ~250 sessions / 365 calendar days ≈ 0.69.
# To convert a requested "business day" lookback into a calendar window we
# multiply by 1/0.69 ≈ 1.45, then round up to 1.6 to give a safety margin
# for short holiday clusters, and add a fixed 14-day buffer to cover the
# longest typical KRX holiday cluster (Lunar New Year / Chuseok week).
_CAL_DAYS_PER_BUSINESS_DAY = 1.6
_HOLIDAY_BUFFER_DAYS = 14


def _business_days_to_calendar_window(business_days: int) -> int:
    """Convert ``business_days`` into a calendar-day lookback for KIS GETs."""
    return int(business_days * _CAL_DAYS_PER_BUSINESS_DAY) + _HOLIDAY_BUFFER_DAYS


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class UniverseItem:
    symbol: str
    name: str
    trade_value: float = 0.0  # 누적 거래대금 (원)
    current_price: float = 0.0
    volume: int = 0


@dataclass
class Fundamentals:
    per: float = 0.0
    pbr: float = 0.0
    eps: float = 0.0
    roe: float = 0.0
    operating_profit: float = 0.0  # 최근 분기 영업이익 (단위: 원)


@dataclass
class DailyBar:
    business_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class InvestorFlowBar:
    business_date: date
    foreign_net: int  # 순매수 (+ buy / - sell), 단위: 주
    institution_net: int


@dataclass
class Recommendation:
    symbol: str
    name: str
    current_price: float
    momentum_score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "current_price": self.current_price,
            "momentum_score": round(self.momentum_score, 3),
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# KIS recommender provider (extends KisProvider with read-only endpoints)
# ---------------------------------------------------------------------------
class RecommenderKisProvider(KisProvider):
    """KisProvider extension that adds endpoints required by the recommender."""

    # Default delay between KIS HTTP calls to stay below the public TPS limit.
    api_call_delay: float = 0.15

    def _sleep(self) -> None:
        if self.api_call_delay > 0:
            time.sleep(self.api_call_delay)

    # -- Universe --------------------------------------------------------
    def get_kospi_volume_universe(self, top_n: int) -> list[UniverseItem]:
        """Return KOSPI stocks ranked by trading value (descending).

        KIS ``volume-rank`` endpoint returns up to ~30 rows per call.
        ``top_n`` is clamped to the API limit.
        """
        data, _ = self._request(
            method="GET",
            path="/uapi/domestic-stock/v1/quotations/volume-rank",
            tr_id="FHPST01710000",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                # 0000 = 전체, 0001 = 코스피, 1001 = 코스닥
                "FID_INPUT_ISCD": "0001",
                "FID_DIV_CLS_CODE": "0",
                # 1 = 거래대금 순, 0 = 평균거래량 순
                "FID_BLNG_CLS_CODE": "1",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": "",
            },
        )
        self._sleep()
        rows = data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        items: list[UniverseItem] = []
        for row in rows:
            symbol = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "").strip()
            if not symbol:
                continue
            items.append(
                UniverseItem(
                    symbol=symbol,
                    name=str(row.get("hts_kor_isnm") or "").strip(),
                    trade_value=_safe_float(row.get("acml_tr_pbmn")),
                    current_price=_safe_float(row.get("stck_prpr")),
                    volume=_safe_int(row.get("acml_vol")),
                )
            )
            if len(items) >= top_n:
                break
        return items

    # -- Fundamentals ----------------------------------------------------
    def get_price_snapshot(self, symbol: str) -> dict[str, float]:
        """Return current price / PER / PBR / EPS for ``symbol``."""
        data, _ = self._request(
            method="GET",
            path="/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        self._sleep()
        out = data.get("output") or {}
        return {
            "current_price": _safe_float(out.get("stck_prpr")),
            "per": _safe_float(out.get("per")),
            "pbr": _safe_float(out.get("pbr")),
            "eps": _safe_float(out.get("eps")),
        }

    def get_financial_ratio(self, symbol: str) -> dict[str, float]:
        """Return latest ROE (%) for ``symbol`` from financial-ratio endpoint."""
        try:
            data, _ = self._request(
                method="GET",
                path="/uapi/domestic-stock/v1/finance/financial-ratio",
                tr_id="FHKST66430300",
                params={
                    "FID_DIV_CLS_CODE": "1",  # 0=년, 1=분기
                    "fid_cond_mrkt_div_code": "J",
                    "fid_input_iscd": symbol,
                },
            )
        except (BrokerError, requests.RequestException):
            return {"roe": 0.0}
        self._sleep()
        rows = data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            return {"roe": 0.0}
        latest = rows[0]
        return {"roe": _safe_float(latest.get("roe_val"))}

    def get_profit_ratio(self, symbol: str) -> dict[str, float]:
        """Return latest quarterly operating profit for ``symbol``."""
        try:
            data, _ = self._request(
                method="GET",
                path="/uapi/domestic-stock/v1/finance/profit-ratio",
                tr_id="FHKST66430400",
                params={
                    "FID_DIV_CLS_CODE": "1",
                    "fid_cond_mrkt_div_code": "J",
                    "fid_input_iscd": symbol,
                },
            )
        except (BrokerError, requests.RequestException):
            return {"operating_profit": 0.0}
        self._sleep()
        rows = data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            return {"operating_profit": 0.0}
        latest = rows[0]
        # KIS profit-ratio response key names differ across API revisions; try
        # the known variants in order. Reference: KIS open-trading-api samples
        # (`bsop_prfi` / `op_prfi` newer, `operating_profit` / `bsop_prti` older).
        candidate_keys = (
            "bsop_prfi",
            "op_prfi",
            "operating_profit",
            "bsop_prti",
        )
        op = 0.0
        for key in candidate_keys:
            if key in latest:
                op = _safe_float(latest.get(key))
                break
        return {"operating_profit": op}

    # -- Price history ---------------------------------------------------
    def get_daily_bars(self, symbol: str, days: int) -> list[DailyBar]:
        """Return daily OHLCV bars for ``symbol`` covering at least ``days`` business days."""
        end_dt = datetime.now(KST).date()
        # Add buffer to compensate for weekends/holidays.
        start_dt = end_dt - timedelta(days=_business_days_to_calendar_window(days))
        data, _ = self._request(
            method="GET",
            path="/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "1",
            },
        )
        self._sleep()
        bars: list[DailyBar] = []
        for row in data.get("output2") or []:
            raw_date = str(row.get("stck_bsop_date") or "")
            if len(raw_date) != 8 or not raw_date.isdigit():
                continue
            try:
                bdate = datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                continue
            close = _safe_float(row.get("stck_clpr"))
            if close <= 0:
                continue
            bars.append(
                DailyBar(
                    business_date=bdate,
                    open=_safe_float(row.get("stck_oprc")),
                    high=_safe_float(row.get("stck_hgpr")),
                    low=_safe_float(row.get("stck_lwpr")),
                    close=close,
                    volume=_safe_int(row.get("acml_vol")),
                )
            )
        bars.sort(key=lambda b: b.business_date)
        return bars[-days:] if days > 0 else bars

    # -- Investor flow ---------------------------------------------------
    def get_investor_flow(self, symbol: str, days: int) -> list[InvestorFlowBar]:
        try:
            data, _ = self._request(
                method="GET",
                path="/uapi/domestic-stock/v1/quotations/inquire-investor",
                tr_id="FHKST01010900",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                },
            )
        except (BrokerError, requests.RequestException):
            return []
        self._sleep()
        rows = data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        flows: list[InvestorFlowBar] = []
        for row in rows[:days]:
            raw_date = str(row.get("stck_bsop_date") or "")
            if len(raw_date) != 8 or not raw_date.isdigit():
                continue
            try:
                bdate = datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                continue
            flows.append(
                InvestorFlowBar(
                    business_date=bdate,
                    foreign_net=_safe_int(row.get("frgn_ntby_qty")),
                    institution_net=_safe_int(row.get("orgn_ntby_qty")),
                )
            )
        flows.sort(key=lambda b: b.business_date)
        return flows

    # -- KOSPI index -----------------------------------------------------
    def get_kospi_daily_closes(self, days: int) -> list[float]:
        """Return KOSPI index closing prices in chronological order."""
        end_dt = datetime.now(KST).date()
        start_dt = end_dt - timedelta(days=_business_days_to_calendar_window(days))
        try:
            data, _ = self._request(
                method="GET",
                path="/uapi/domestic-stock/v1/quotations/inquire-index-daily-price",
                tr_id="FHPUP02120000",
                params={
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": "0001",  # KOSPI
                    "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                },
            )
        except (BrokerError, requests.RequestException):
            return []
        self._sleep()
        rows = data.get("output2") or data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        closes: list[tuple[date, float]] = []
        for row in rows:
            raw_date = str(row.get("stck_bsop_date") or row.get("bsop_date") or "")
            if len(raw_date) != 8 or not raw_date.isdigit():
                continue
            try:
                bdate = datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                continue
            close = _safe_float(row.get("bstp_nmix_prpr") or row.get("stck_clpr"))
            if close <= 0:
                continue
            closes.append((bdate, close))
        closes.sort(key=lambda x: x[0])
        return [c for _, c in closes[-days:]] if days > 0 else [c for _, c in closes]


# ---------------------------------------------------------------------------
# Technical indicators (pure functions, easy to unit-test)
# ---------------------------------------------------------------------------
def simple_moving_average(values: list[float], window: int) -> float:
    if window <= 0 or len(values) < window:
        return 0.0
    return sum(values[-window:]) / window


def count_up_days(bars: list[DailyBar], lookback: int) -> int:
    """Count days where close > open within the last ``lookback`` bars."""
    if lookback <= 0:
        return 0
    target = bars[-lookback:] if len(bars) >= lookback else bars
    return sum(1 for b in target if b.close > b.open)


def has_pullback_then_rebound(
    bars: list[DailyBar], pullback_days: int, rebound_days: int
) -> bool:
    """True when the last ``rebound_days`` bars are strictly rising after a
    period of ``pullback_days`` bars whose first close is higher than the last
    close (i.e. there was a recent dip)."""
    if pullback_days <= 0 or rebound_days <= 0:
        return False
    needed = pullback_days + rebound_days
    if len(bars) < needed + 1:
        return False
    window = bars[-(needed + 1):]
    pullback_start = window[0].close
    pullback_end = window[pullback_days].close
    if pullback_end >= pullback_start:
        return False
    rebound_segment = window[pullback_days:]
    for prev, curr in zip(rebound_segment, rebound_segment[1:]):
        if curr.close <= prev.close:
            return False
    return True


def latest_volume_surge_ratio(bars: list[DailyBar], window: int) -> float:
    if window <= 0 or len(bars) <= window:
        return 0.0
    recent = bars[-(window + 1):-1]
    avg_volume = sum(b.volume for b in recent) / len(recent) if recent else 0.0
    if avg_volume <= 0:
        return 0.0
    return bars[-1].volume / avg_volume


def compute_rsi(closes: list[float], period: int) -> float:
    if period <= 0 or len(closes) <= period:
        return 0.0
    gains = 0.0
    losses = 0.0
    for prev, curr in zip(closes[-(period + 1):-1], closes[-period:]):
        change = curr - prev
        if change >= 0:
            gains += change
        else:
            losses += -change
    if gains + losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def golden_cross_status(
    closes: list[float], short_window: int, mid_window: int, long_window: int
) -> tuple[bool, float]:
    """Return (qualified, ma_disparity_pct).

    Qualified when:
      * short MA >= mid MA >= long MA (alignment / start of alignment), AND
      * long MA > previous long MA (long MA rising).
    Disparity = (last_close / long_ma - 1) * 100.
    """
    if len(closes) < max(short_window, mid_window, long_window) + 1:
        return False, 0.0
    short_ma = simple_moving_average(closes, short_window)
    mid_ma = simple_moving_average(closes, mid_window)
    long_ma = simple_moving_average(closes, long_window)
    long_ma_prev = simple_moving_average(closes[:-1], long_window)
    qualified = (
        short_ma > 0
        and mid_ma > 0
        and long_ma > 0
        and short_ma >= mid_ma >= long_ma
        and long_ma >= long_ma_prev
    )
    last = closes[-1]
    disparity = (last / long_ma - 1.0) * 100.0 if long_ma > 0 else 0.0
    return qualified, disparity


# ---------------------------------------------------------------------------
# Recommendation pipeline
# ---------------------------------------------------------------------------
@dataclass
class RecommenderConfig:
    phase: int = 3
    top_n: int = 30
    max_per: float = 100.0
    min_roe: float = 8.0
    lookback_n: int = 5
    up_days_m: int = 3
    pullback_x: int = 3
    rebound_y: int = 2
    volume_window: int = 20
    volume_surge_ratio: float = 2.0
    ma_short: int = 5
    ma_mid: int = 20
    ma_long: int = 60
    max_disparity_pct: float = 15.0
    invflow_days_lookback: int = 5
    invflow_days_k: int = 3
    rsi_period: int = 14
    rsi_max: float = 70.0
    kospi_ma_window: int = 20
    defensive_max_recommend: int = 5
    max_recommend: int = 20


def build_macro_context(provider: RecommenderKisProvider, cfg: RecommenderConfig) -> dict[str, Any]:
    """Determine if KOSPI is above its ``kospi_ma_window``-day moving average."""
    if cfg.phase < 3:
        return {"available": False, "uptrend": True, "max_recommend": cfg.max_recommend}
    closes = provider.get_kospi_daily_closes(cfg.kospi_ma_window + 5)
    if len(closes) < cfg.kospi_ma_window:
        return {"available": False, "uptrend": True, "max_recommend": cfg.max_recommend}
    ma = simple_moving_average(closes, cfg.kospi_ma_window)
    uptrend = closes[-1] >= ma
    max_recommend = cfg.max_recommend if uptrend else min(cfg.max_recommend, cfg.defensive_max_recommend)
    return {
        "available": True,
        "uptrend": uptrend,
        "kospi_close": closes[-1],
        "kospi_ma": ma,
        "max_recommend": max_recommend,
    }


def evaluate_candidate(
    provider: RecommenderKisProvider,
    item: UniverseItem,
    cfg: RecommenderConfig,
    macro: dict[str, Any],
) -> Recommendation | None:
    """Apply all enabled phase filters to ``item`` and return a recommendation
    if it qualifies. Returns ``None`` otherwise."""

    reasons: list[str] = []
    score = 0.0

    # ---- Phase 1: fundamentals ----------------------------------------
    snapshot = provider.get_price_snapshot(item.symbol)
    current_price = snapshot["current_price"] or item.current_price
    per = snapshot["per"]
    pbr = snapshot["pbr"]
    if current_price <= 0:
        return None
    if cfg.max_per > 0 and per > 0 and per >= cfg.max_per:
        return None

    fundamentals = Fundamentals(per=per, pbr=pbr, eps=snapshot["eps"])
    if cfg.phase >= 1:
        ratio = provider.get_financial_ratio(item.symbol)
        profit = provider.get_profit_ratio(item.symbol)
        fundamentals.roe = ratio.get("roe", 0.0)
        fundamentals.operating_profit = profit.get("operating_profit", 0.0)
        if fundamentals.operating_profit < 0:
            return None
        if fundamentals.roe >= cfg.min_roe:
            score += 1.0
            reasons.append(f"ROE {fundamentals.roe:.1f}%")

    # ---- Phase 1: momentum (price pattern + volume surge) -------------
    bars = provider.get_daily_bars(
        item.symbol, max(cfg.lookback_n, cfg.volume_window, cfg.ma_long) + 5
    )
    if len(bars) < max(cfg.lookback_n, cfg.pullback_x + cfg.rebound_y + 1):
        return None

    up_days = count_up_days(bars, cfg.lookback_n)
    if up_days < cfg.up_days_m:
        return None
    score += 1.0
    reasons.append(f"{cfg.lookback_n}일 중 {up_days}일 양봉")

    if has_pullback_then_rebound(bars, cfg.pullback_x, cfg.rebound_y):
        score += 1.0
        reasons.append(f"{cfg.pullback_x}일 조정 후 {cfg.rebound_y}일 연속 상승")

    surge = latest_volume_surge_ratio(bars, cfg.volume_window)
    if surge < cfg.volume_surge_ratio:
        return None
    last_bar = bars[-1]
    if last_bar.close <= last_bar.open:
        return None
    score += 1.0
    reasons.append(f"거래량 {surge:.1f}배 급증 + 양봉")

    closes = [b.close for b in bars]

    # ---- Phase 2: moving-average alignment + investor flow ------------
    if cfg.phase >= 2:
        qualified, disparity = golden_cross_status(
            closes, cfg.ma_short, cfg.ma_mid, cfg.ma_long
        )
        if not qualified:
            return None
        if cfg.max_disparity_pct > 0 and disparity > cfg.max_disparity_pct:
            return None
        score += 1.0
        reasons.append(
            f"이평선 정배열({cfg.ma_short}/{cfg.ma_mid}/{cfg.ma_long}), 이격도 {disparity:.1f}%"
        )

        flows = provider.get_investor_flow(item.symbol, cfg.invflow_days_lookback)
        if flows:
            dual_days = sum(
                1 for f in flows[-cfg.invflow_days_lookback:]
                if f.foreign_net > 0 and f.institution_net > 0
            )
            if dual_days >= cfg.invflow_days_k:
                score += 1.0
                reasons.append(
                    f"외국인·기관 쌍끌이 순매수 {dual_days}일"
                )

    # ---- Phase 3: RSI guard -------------------------------------------
    if cfg.phase >= 3:
        rsi = compute_rsi(closes, cfg.rsi_period)
        if rsi >= cfg.rsi_max:
            return None
        if 30 <= rsi < cfg.rsi_max:
            reasons.append(f"RSI {rsi:.1f} (과매수 아님)")

    # ---- Macro defensive adjustment -----------------------------------
    if cfg.phase >= 3 and macro.get("available") and not macro.get("uptrend"):
        # In a downtrend market, prefer lower-PER value/defensive names.
        if pbr > 0 and pbr <= 1.5:
            score += 0.5
            reasons.append("코스피 하락장 - 저PBR 방어주")

    return Recommendation(
        symbol=item.symbol,
        name=item.name,
        current_price=current_price,
        momentum_score=score,
        reasons=reasons,
    )


def recommend(
    provider: RecommenderKisProvider, cfg: RecommenderConfig
) -> tuple[list[Recommendation], dict[str, Any]]:
    macro = build_macro_context(provider, cfg)
    universe = provider.get_kospi_volume_universe(cfg.top_n)

    recommendations: list[Recommendation] = []
    for item in universe:
        try:
            rec = evaluate_candidate(provider, item, cfg, macro)
        except (BrokerError, requests.RequestException) as exc:
            print(f"[warn] {item.symbol} {item.name} evaluation failed: {exc}", file=sys.stderr)
            continue
        if rec is not None:
            recommendations.append(rec)

    recommendations.sort(key=lambda r: r.momentum_score, reverse=True)
    max_recommend = int(macro.get("max_recommend") or cfg.max_recommend)
    if max_recommend > 0:
        recommendations = recommendations[:max_recommend]
    return recommendations, macro


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def format_markdown_table(
    recommendations: list[Recommendation], macro: dict[str, Any]
) -> str:
    lines: list[str] = []
    if macro.get("available"):
        regime = "Uptrend" if macro.get("uptrend") else "Downtrend (defensive)"
        lines.append(
            f"_Macro: KOSPI close={macro.get('kospi_close'):.2f}, "
            f"MA={macro.get('kospi_ma'):.2f}, regime={regime}_"
        )
        lines.append("")
    if not recommendations:
        lines.append("_No stocks matched all filters._")
        return "\n".join(lines)
    lines.append("| # | Symbol | Name | Price | Score | Reasons |")
    lines.append("|---|--------|------|-------|-------|---------|")
    for idx, rec in enumerate(recommendations, start=1):
        reasons = "; ".join(rec.reasons) if rec.reasons else "-"
        lines.append(
            f"| {idx} | `{rec.symbol}` | {rec.name} | "
            f"{rec.current_price:,.0f} | {rec.momentum_score:.2f} | {reasons} |"
        )
    return "\n".join(lines)


def write_json_output(
    recommendations: list[Recommendation], macro: dict[str, Any], output_path: str
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(KST).isoformat(),
        "macro": {
            k: v
            for k, v in macro.items()
            if isinstance(v, (int, float, str, bool, type(None)))
        },
        "recommendations": [r.to_dict() for r in recommendations],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_provider(account_config: dict[str, Any]) -> RecommenderKisProvider:
    provider = str(account_config.get("provider", "")).lower()
    params = account_config.get("params") or {}
    if provider == "kis":
        return RecommenderKisProvider(params)
    raise ConfigError(f"Unsupported provider: {provider}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Daily KOSPI stock recommender (multi-phase pipeline using KIS Open API)."
        )
    )
    parser.add_argument("--config", required=True, help="Path to account config JSON")

    parser.add_argument("--phase", type=int, default=3, choices=[1, 2, 3],
                        help="Pipeline phase to execute (1, 2 or 3)")
    parser.add_argument("--top-n", type=int, default=30,
                        help="Universe size by KOSPI trade-value rank (default: 30)")
    parser.add_argument("--max-per", type=float, default=100.0,
                        help="Exclude stocks with PER >= this value (0 to disable)")
    parser.add_argument("--min-roe", type=float, default=8.0,
                        help="Reward stocks with ROE >= this percentage")

    parser.add_argument("--lookback-n", type=int, default=5,
                        help="Recent business days window for momentum check (N)")
    parser.add_argument("--up-days-m", type=int, default=3,
                        help="Required up-days within --lookback-n (M)")
    parser.add_argument("--pullback-x", type=int, default=3,
                        help="Number of pullback days before rebound (X)")
    parser.add_argument("--rebound-y", type=int, default=2,
                        help="Consecutive rebound days after pullback (Y)")

    parser.add_argument("--volume-window", type=int, default=20,
                        help="Window size for average volume calculation")
    parser.add_argument("--volume-surge-ratio", type=float, default=2.0,
                        help="Latest volume / window avg must exceed this ratio (Z)")

    parser.add_argument("--ma-short", type=int, default=5,
                        help="Short moving-average window (default: 5)")
    parser.add_argument("--ma-mid", type=int, default=20,
                        help="Mid moving-average window (default: 20)")
    parser.add_argument("--ma-long", type=int, default=60,
                        help="Long moving-average window (default: 60)")
    parser.add_argument("--max-disparity-pct", type=float, default=15.0,
                        help="Reject if (close/long_ma - 1) * 100 exceeds this (over-extension)")

    parser.add_argument("--invflow-days-lookback", type=int, default=5,
                        help="Lookback business days for investor flow")
    parser.add_argument("--invflow-days-k", type=int, default=3,
                        help="Required dual (foreign+institution) net-buy days (K)")

    parser.add_argument("--rsi-period", type=int, default=14,
                        help="RSI period (default: 14)")
    parser.add_argument("--rsi-max", type=float, default=70.0,
                        help="Exclude stocks with RSI >= this value")

    parser.add_argument("--kospi-ma-window", type=int, default=20,
                        help="KOSPI macro filter moving-average window")
    parser.add_argument("--defensive-max-recommend", type=int, default=5,
                        help="Cap recommended stocks when KOSPI is below its MA")
    parser.add_argument("--max-recommend", type=int, default=20,
                        help="Maximum number of recommended stocks")

    parser.add_argument("--api-call-delay", type=float, default=0.15,
                        help="Sleep seconds between KIS API calls (rate-limit)")

    parser.add_argument("--recommend-json-output",
                        help="Optional path to write recommendations as JSON")
    parser.add_argument("--token-state-output",
                        help="Write refreshed KIS token state JSON (compatible with auto_floor_sell)")
    parser.add_argument("--access-token",
                        help="Cached KIS access token to reuse when still within the reuse window")
    parser.add_argument("--access-token-issued-at",
                        help='UTC ISO-8601 timestamp for --access-token issue time')
    parser.add_argument("--token-reuse-hours", type=float, default=None,
                        help="KIS cached token reuse window in hours (default: provider config or 21)")

    return parser.parse_args(argv)


def args_to_config(args: argparse.Namespace) -> RecommenderConfig:
    return RecommenderConfig(
        phase=args.phase,
        top_n=args.top_n,
        max_per=args.max_per,
        min_roe=args.min_roe,
        lookback_n=args.lookback_n,
        up_days_m=args.up_days_m,
        pullback_x=args.pullback_x,
        rebound_y=args.rebound_y,
        volume_window=args.volume_window,
        volume_surge_ratio=args.volume_surge_ratio,
        ma_short=args.ma_short,
        ma_mid=args.ma_mid,
        ma_long=args.ma_long,
        max_disparity_pct=args.max_disparity_pct,
        invflow_days_lookback=args.invflow_days_lookback,
        invflow_days_k=args.invflow_days_k,
        rsi_period=args.rsi_period,
        rsi_max=args.rsi_max,
        kospi_ma_window=args.kospi_ma_window,
        defensive_max_recommend=args.defensive_max_recommend,
        max_recommend=args.max_recommend,
    )


def run_daily_recommender(
    config_path: str,
    cfg: RecommenderConfig,
    api_call_delay: float,
    recommend_json_output: str | None,
    token_state_output: str | None = None,
    access_token: str | None = None,
    access_token_issued_at: str | None = None,
    token_reuse_hours: float | None = None,
) -> int:
    account_config = load_account_config(config_path)
    params = account_config.setdefault("params", {})
    if access_token is not None:
        params["access_token"] = access_token
    if access_token_issued_at is not None:
        params["access_token_issued_at"] = access_token_issued_at
    if token_reuse_hours is not None:
        params["token_reuse_hours"] = token_reuse_hours

    provider = build_provider(account_config)
    if isinstance(provider, RecommenderKisProvider):
        provider.api_call_delay = max(0.0, float(api_call_delay))

    recommendations, macro = recommend(provider, cfg)

    print("## KIS Daily Recommendations")
    print(f"_Generated at {datetime.now(KST).isoformat()} (phase={cfg.phase})_")
    print()
    print(format_markdown_table(recommendations, macro))

    if recommend_json_output:
        write_json_output(recommendations, macro, recommend_json_output)

    write_token_state_output(provider, token_state_output)
    return 0


def _validate_args(args: argparse.Namespace) -> None:
    if args.top_n <= 0:
        raise SystemExit("--top-n must be greater than 0")
    if args.lookback_n <= 0:
        raise SystemExit("--lookback-n must be greater than 0")
    if not (0 < args.up_days_m <= args.lookback_n):
        raise SystemExit("--up-days-m must satisfy 0 < M <= --lookback-n")
    if args.pullback_x <= 0 or args.rebound_y <= 0:
        raise SystemExit("--pullback-x and --rebound-y must be greater than 0")
    if args.volume_window <= 0:
        raise SystemExit("--volume-window must be greater than 0")
    if args.volume_surge_ratio <= 0:
        raise SystemExit("--volume-surge-ratio must be greater than 0")
    if not (args.ma_short < args.ma_mid < args.ma_long):
        raise SystemExit("Moving averages must satisfy ma_short < ma_mid < ma_long")
    if args.invflow_days_k < 0 or args.invflow_days_lookback <= 0:
        raise SystemExit("Investor flow days must be valid (K>=0, lookback>0)")
    if not (0 < args.rsi_max <= 100):
        raise SystemExit("--rsi-max must be in (0, 100]")
    if args.kospi_ma_window <= 0:
        raise SystemExit("--kospi-ma-window must be greater than 0")
    if args.max_recommend <= 0:
        raise SystemExit("--max-recommend must be greater than 0")
    if args.api_call_delay < 0:
        raise SystemExit("--api-call-delay must be >= 0")


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_args(args)
    cfg = args_to_config(args)
    try:
        return run_daily_recommender(
            config_path=args.config,
            cfg=cfg,
            api_call_delay=args.api_call_delay,
            recommend_json_output=args.recommend_json_output,
            token_state_output=args.token_state_output,
            access_token=args.access_token,
            access_token_issued_at=args.access_token_issued_at,
            token_reuse_hours=args.token_reuse_hours,
        )
    except (ConfigError, BrokerError, requests.RequestException) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
