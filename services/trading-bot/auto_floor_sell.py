#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


KST = ZoneInfo("Asia/Seoul")


def _safe_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


class BrokerError(RuntimeError):
    pass


class ConfigError(ValueError):
    pass


@dataclass
class Position:
    symbol: str
    name: str
    quantity: int
    orderable_quantity: int
    average_entry_price: float


@dataclass
class SellDecision:
    symbol: str
    name: str
    average_entry_price: float
    last_trade_date: date
    highest_price_since_trade: float
    current_price: float
    drawdown_ratio: float
    threshold_ratio: float
    should_sell: bool
    orderable_quantity: int


class BrokerProvider(ABC):
    @abstractmethod
    def is_market_open(self, now_kst: datetime) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_positions(self) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    def get_last_trade_date(self, symbol: str, lookback_days: int) -> date | None:
        raise NotImplementedError

    @abstractmethod
    def get_highest_price_since(self, symbol: str, start_date: date, end_date: date) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_current_price(self, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def market_sell(self, symbol: str, quantity: int) -> dict[str, Any]:
        raise NotImplementedError


class KisProvider(BrokerProvider):
    def __init__(self, params: dict[str, Any]) -> None:
        self.api_key = params.get("api_key") or os.getenv("KIS_API_KEY")
        self.api_secret = params.get("api_secret") or os.getenv("KIS_API_SECRET")
        self.cano = params.get("cano") or os.getenv("KIS_CANO")
        self.acnt_prdt_cd = params.get("acnt_prdt_cd") or os.getenv("KIS_ACNT_PRDT_CD")
        env_name = (params.get("env") or os.getenv("KIS_ENV") or "vps").lower()
        if env_name not in {"prod", "real", "vps", "demo"}:
            raise ConfigError("KIS env must be one of prod/real/vps/demo")
        self.is_demo = env_name in {"vps", "demo"}
        self.base_url = (
            "https://openapivts.koreainvestment.com:29443"
            if self.is_demo
            else "https://openapi.koreainvestment.com:9443"
        )

        required = {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "cano": self.cano,
            "acnt_prdt_cd": self.acnt_prdt_cd,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ConfigError(f"Missing KIS params: {', '.join(missing)}")

        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _issue_token(self) -> None:
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.api_key,
            "appsecret": self.api_secret,
        }
        response = requests.post(url, json=body, timeout=15)
        response.raise_for_status()
        data = response.json()
        access_token = data.get("access_token")
        if not access_token:
            raise BrokerError(f"KIS token issue failed: {data}")
        self._token = access_token
        self._token_expires_at = time.time() + 60 * 60

    def _auth_header(self) -> dict[str, str]:
        if not self._token or time.time() >= self._token_expires_at - 60:
            self._issue_token()
        return {
            "authorization": f"Bearer {self._token}",
            "appkey": str(self.api_key),
            "appsecret": str(self.api_secret),
            "custtype": "P",
        }

    def _hashkey(self, body: dict[str, Any]) -> str:
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": str(self.api_key),
            "appsecret": str(self.api_secret),
        }
        response = requests.post(url, headers=headers, json=body, timeout=15)
        response.raise_for_status()
        data = response.json()
        hash_value = data.get("HASH")
        if not hash_value:
            raise BrokerError(f"KIS hashkey issue failed: {data}")
        return hash_value

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        tr_cont: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        headers = {
            **self._auth_header(),
            "tr_id": tr_id,
            "tr_cont": tr_cont,
        }
        if method.upper() == "POST":
            payload = body or {}
            headers["content-type"] = "application/json; charset=utf-8"
            headers["hashkey"] = self._hashkey(payload)
            response = requests.post(
                f"{self.base_url}{path}",
                headers=headers,
                json=payload,
                timeout=20,
            )
        else:
            response = requests.get(
                f"{self.base_url}{path}",
                headers=headers,
                params=params or {},
                timeout=20,
            )
        response.raise_for_status()
        data = response.json()
        rt_cd = str(data.get("rt_cd", ""))
        if rt_cd and rt_cd != "0":
            msg = data.get("msg1") or data.get("msg_cd") or "Unknown KIS error"
            raise BrokerError(f"KIS request failed: {path} ({msg})")
        return data, response.headers

    def _balance_tr_id(self) -> str:
        return "VTTC8434R" if self.is_demo else "TTTC8434R"

    def _daily_ccld_tr_id(self) -> str:
        return "VTTC0081R" if self.is_demo else "TTTC0081R"

    def _order_sell_tr_id(self) -> str:
        return "VTTC0011U" if self.is_demo else "TTTC0011U"

    def is_market_open(self, now_kst: datetime) -> bool:
        if now_kst.weekday() >= 5:
            return False
        hhmm = now_kst.hour * 100 + now_kst.minute
        if hhmm < 900 or hhmm > 1530:
            return False

        today = now_kst.strftime("%Y%m%d")
        data, _ = self._request(
            method="GET",
            path="/uapi/domestic-stock/v1/quotations/chk-holiday",
            tr_id="CTCA0903R",
            params={"BASS_DT": today, "CTX_AREA_FK": "", "CTX_AREA_NK": ""},
        )
        output = data.get("output") or []
        if isinstance(output, dict):
            output = [output]
        for row in output:
            if str(row.get("bass_dt", "")) == today:
                return str(row.get("opnd_yn", "N")) == "Y"
        return False

    def list_positions(self) -> list[Position]:
        positions: list[Position] = []
        fk = ""
        nk = ""
        tr_cont = ""

        while True:
            data, headers = self._request(
                method="GET",
                path="/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id=self._balance_tr_id(),
                tr_cont=tr_cont,
                params={
                    "CANO": self.cano,
                    "ACNT_PRDT_CD": self.acnt_prdt_cd,
                    "AFHR_FLPR_YN": "N",
                    "OFL_YN": "",
                    "INQR_DVSN": "02",
                    "UNPR_DVSN": "01",
                    "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN": "00",
                    "CTX_AREA_FK100": fk,
                    "CTX_AREA_NK100": nk,
                },
            )

            for row in data.get("output1") or []:
                quantity = _safe_int(row.get("hldg_qty"))
                if quantity <= 0:
                    continue
                symbol = str(row.get("pdno") or "").strip()
                if not symbol:
                    continue
                orderable_quantity = _safe_int(row.get("ord_psbl_qty"))
                if orderable_quantity <= 0:
                    orderable_quantity = quantity
                positions.append(
                    Position(
                        symbol=symbol,
                        name=str(row.get("prdt_name") or "").strip(),
                        quantity=quantity,
                        orderable_quantity=orderable_quantity,
                        average_entry_price=_safe_float(row.get("pchs_avg_pric")),
                    )
                )

            tr_next = headers.get("tr_cont", "")
            if tr_next not in {"M", "F"}:
                break
            body = data.get("output2") or {}
            fk = str(body.get("ctx_area_fk100") or "")
            nk = str(body.get("ctx_area_nk100") or "")
            tr_cont = "N"
            time.sleep(0.2)

        return positions

    def get_last_trade_date(self, symbol: str, lookback_days: int) -> date | None:
        end_dt = datetime.now(KST).date()
        start_dt = end_dt - timedelta(days=lookback_days)

        latest: date | None = None
        fk = ""
        nk = ""
        tr_cont = ""

        while True:
            data, headers = self._request(
                method="GET",
                path="/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=self._daily_ccld_tr_id(),
                tr_cont=tr_cont,
                params={
                    "CANO": self.cano,
                    "ACNT_PRDT_CD": self.acnt_prdt_cd,
                    "INQR_STRT_DT": start_dt.strftime("%Y%m%d"),
                    "INQR_END_DT": end_dt.strftime("%Y%m%d"),
                    "SLL_BUY_DVSN_CD": "00",
                    "PDNO": symbol,
                    "CCLD_DVSN": "01",
                    "INQR_DVSN": "00",
                    "INQR_DVSN_3": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": "",
                    "INQR_DVSN_1": "",
                    "CTX_AREA_FK100": fk,
                    "CTX_AREA_NK100": nk,
                    "EXCG_ID_DVSN_CD": "KRX",
                },
            )

            for row in data.get("output1") or []:
                if _safe_int(row.get("tot_ccld_qty")) <= 0:
                    continue
                ord_dt = str(row.get("ord_dt") or "")
                if len(ord_dt) != 8 or not ord_dt.isdigit():
                    continue
                candidate = datetime.strptime(ord_dt, "%Y%m%d").date()
                if latest is None or candidate > latest:
                    latest = candidate

            tr_next = headers.get("tr_cont", "")
            if tr_next not in {"M", "F"}:
                break
            body = data.get("output2") or {}
            fk = str(body.get("ctx_area_fk100") or "")
            nk = str(body.get("ctx_area_nk100") or "")
            tr_cont = "N"
            time.sleep(0.2)

        return latest

    def get_highest_price_since(self, symbol: str, start_date: date, end_date: date) -> float:
        if start_date > end_date:
            return 0.0

        highest = 0.0
        cursor = start_date

        while cursor <= end_date:
            chunk_end = min(cursor + timedelta(days=99), end_date)
            data, _ = self._request(
                method="GET",
                path="/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                tr_id="FHKST03010100",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": cursor.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": chunk_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "1",
                },
            )
            for row in data.get("output2") or []:
                price = _safe_float(row.get("stck_hgpr"))
                if price > highest:
                    highest = price

            cursor = chunk_end + timedelta(days=1)
            time.sleep(0.15)

        return highest

    def get_current_price(self, symbol: str) -> float:
        data, _ = self._request(
            method="GET",
            path="/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        output = data.get("output") or {}
        return _safe_float(output.get("stck_prpr"))

    def market_sell(self, symbol: str, quantity: int) -> dict[str, Any]:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        data, _ = self._request(
            method="POST",
            path="/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=self._order_sell_tr_id(),
            body={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": symbol,
                "ORD_DVSN": "01",
                "ORD_QTY": str(quantity),
                "ORD_UNPR": "0",
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "",
                "CNDT_PRIC": "",
            },
        )
        return data.get("output") or {}


def load_account_config(config_path: str) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON config: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("Config root must be an object")
    if "provider" not in data:
        raise ConfigError("Config must include 'provider'")
    if "params" in data and not isinstance(data["params"], dict):
        raise ConfigError("Config 'params' must be an object")
    data.setdefault("params", {})
    return data


def build_provider(account_config: dict[str, Any]) -> BrokerProvider:
    provider = str(account_config.get("provider", "")).lower()
    params = account_config.get("params") or {}
    if provider == "kis":
        return KisProvider(params)
    raise ConfigError(f"Unsupported provider: {provider}")


def evaluate_sell_decisions(
    broker: BrokerProvider,
    sell_ratio: float,
    lookback_days: int,
    now_kst: datetime,
) -> list[SellDecision]:
    decisions: list[SellDecision] = []
    positions = broker.list_positions()

    for position in positions:
        if position.orderable_quantity <= 0:
            continue

        last_trade_date = broker.get_last_trade_date(position.symbol, lookback_days)
        if last_trade_date is None:
            continue

        highest = broker.get_highest_price_since(position.symbol, last_trade_date, now_kst.date())
        current = broker.get_current_price(position.symbol)
        if highest <= 0 or current <= 0:
            continue

        drawdown = (highest - current) / highest
        decisions.append(
            SellDecision(
                symbol=position.symbol,
                name=position.name,
                average_entry_price=position.average_entry_price,
                last_trade_date=last_trade_date,
                highest_price_since_trade=highest,
                current_price=current,
                drawdown_ratio=drawdown,
                threshold_ratio=sell_ratio,
                should_sell=drawdown >= sell_ratio,
                orderable_quantity=position.orderable_quantity,
            )
        )

    return decisions


def run_auto_floor_sell(
    config_path: str,
    sell_ratio: float,
    lookback_days: int,
    dry_run: bool,
    read_only: bool,
) -> int:
    account_config = load_account_config(config_path)
    broker = build_provider(account_config)
    now_kst = datetime.now(KST)

    if not read_only and not broker.is_market_open(now_kst):
        print("[skip] Market is not open. Sell logic is not executed.")
        return 0

    decisions = evaluate_sell_decisions(broker, sell_ratio, lookback_days, now_kst)

    if not decisions:
        print("[done] No eligible positions for auto floor sell.")
        return 0

    for decision in decisions:
        print(
            "[check] "
            f"{decision.symbol} {decision.name} "
            f"avg={decision.average_entry_price:.2f} "
            f"last_trade={decision.last_trade_date.isoformat()} "
            f"H={decision.highest_price_since_trade:.2f} "
            f"current={decision.current_price:.2f} "
            f"drawdown={decision.drawdown_ratio:.4f} "
            f"threshold={decision.threshold_ratio:.4f}"
        )

        if not decision.should_sell:
            continue

        if dry_run or read_only:
            mode = "read-only" if read_only else "dry-run"
            print(
                f"[{mode}] sell trigger "
                f"{decision.symbol} qty={decision.orderable_quantity}"
            )
            continue

        order_result = broker.market_sell(decision.symbol, decision.orderable_quantity)
        order_no = order_result.get("ODNO") or order_result.get("odno") or "-"
        print(
            "[sell] executed "
            f"{decision.symbol} qty={decision.orderable_quantity} order_no={order_no}"
        )

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto floor sell: sell if drawdown from highest price exceeds threshold"
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to account config JSON (provider + params)",
    )
    parser.add_argument(
        "--sell-ratio",
        type=float,
        default=0.10,
        help="Drawdown threshold ratio (default: 0.10)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=365,
        help="Max lookback days to search last trade date (default: 365)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and print signals without sending orders",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Query and evaluate only (ignores market-open gate, never sends orders)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sell_ratio <= 0:
        raise SystemExit("--sell-ratio must be greater than 0")
    if args.sell_ratio >= 1:
        raise SystemExit("--sell-ratio must be less than 1")
    if args.lookback_days <= 0:
        raise SystemExit("--lookback-days must be greater than 0")

    try:
        return run_auto_floor_sell(
            config_path=args.config,
            sell_ratio=args.sell_ratio,
            lookback_days=args.lookback_days,
            dry_run=args.dry_run,
            read_only=args.read_only,
        )
    except (ConfigError, BrokerError, requests.RequestException) as exc:
        print(f"[error] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
