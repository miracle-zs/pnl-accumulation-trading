"""Binance USDⓈ-M Futures REST adapter.

This adapter is deliberately constructed with an injected ``httpx.AsyncClient``.
The caller owns its lifetime and supplies API credentials; no request is
made merely by constructing the gateway.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx

from ...application.execution_models import (
    ExchangeOrderSnapshot,
    ExchangeOrderStatus,
    MarketOrderRequest,
    SubmissionCertainty,
    SubmissionResult,
)
from ...application.market_models import (
    ExchangeInstrument,
    FifteenMinuteCloseSnapshot,
    InstrumentRules,
    MarketQuantityRules,
)
from ...application.position_models import ExchangePosition
from ...domain.models import TradeFillRecord, ZERO


BINANCE_USDM_FUTURES_URL = "https://fapi.binance.com"
BINANCE_RECV_WINDOW_MS = 5_000
EXCHANGE_INFO_CACHE_TTL_MS = 86_400_000
logger = logging.getLogger(__name__)


class BinanceApiError(RuntimeError):
    def __init__(self, status_code: int, code: int, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"HTTP {status_code}; code={code}; {message}")

    @property
    def execution_unknown(self) -> bool:
        if self.code in {-1000, -1001, -1003, -1006, -1007, -1008, -1016}:
            return True
        if self.status_code == 503:
            message = self.message.lower()
            if "service unavailable" in message or "internal error; unable to process" in message:
                return False
            return True
        return (
            self.status_code == 408
            or self.status_code >= 500
            or self.status_code in {418, 429}
        )


class BinanceProtocolError(RuntimeError):
    """The exchange response could not be interpreted safely."""


class BinanceFuturesGateway:
    """ExchangeGatewayPort implementation for one-way USDⓈ-M futures."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        api_secret: str,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret must not be empty")
        self._client = client
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._exchange_info_cache: tuple[int, tuple[ExchangeInstrument, ...]] | None = None

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules:
        instruments = await self.get_exchange_info_snapshot()
        instrument = next((item for item in instruments if item.symbol == symbol), None)
        if instrument is None:
            raise LookupError(f"exchangeInfo does not contain {symbol}")
        if instrument.rules is None:
            raise LookupError(f"exchangeInfo does not contain usable market rules for {symbol}")
        return instrument.rules

    async def get_exchange_info_snapshot(self) -> tuple[ExchangeInstrument, ...]:
        """Fetch the complete symbol catalog, preserving rows with unusable order filters."""

        now_ms = self._clock_ms()
        cached = self._exchange_info_cache
        if cached is not None and now_ms - cached[0] < EXCHANGE_INFO_CACHE_TTL_MS:
            return cached[1]

        payload = await self._request_json("GET", "/fapi/v1/exchangeInfo")
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise BinanceProtocolError("exchangeInfo response is missing its symbols list")
        if not payload["symbols"]:
            raise BinanceProtocolError("exchangeInfo returned an empty symbols list")

        instruments: list[ExchangeInstrument] = []
        for index, row in enumerate(payload["symbols"]):
            if not isinstance(row, dict):
                raise BinanceProtocolError(f"exchangeInfo row {index} must be an object")
            symbol = _required_text(row.get("symbol"), "symbol")
            base_asset = _required_text(row.get("baseAsset"), "baseAsset")
            quote_asset = _required_text(row.get("quoteAsset"), "quoteAsset")
            margin_asset = _required_text(row.get("marginAsset"), "marginAsset")
            contract_type = _required_text(row.get("contractType"), "contractType")
            status = _required_text(row.get("status"), "status")
            try:
                rules = _parse_instrument_rules(
                    row,
                    symbol=symbol,
                    contract_type=contract_type,
                    status=status,
                    quote_asset=quote_asset,
                    margin_asset=margin_asset,
                )
            except (BinanceProtocolError, ValueError):
                rules = None
            instruments.append(
                ExchangeInstrument(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_asset=quote_asset,
                    margin_asset=margin_asset,
                    contract_type=contract_type,
                    status=status,
                    rules=rules,
                )
            )

        loaded_at_ms = self._clock_ms()
        result = tuple(instruments)
        self._exchange_info_cache = (loaded_at_ms, result)
        return result

    async def get_fifteen_minute_close(
        self,
        symbol: str,
        slot_start_ms: int,
    ) -> FifteenMinuteCloseSnapshot:
        close_time_ms = slot_start_ms + 899_999
        payload = await self._request_json(
            "GET",
            "/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": "15m",
                "startTime": str(slot_start_ms),
                "endTime": str(close_time_ms),
                "limit": "1",
            },
        )
        if not isinstance(payload, list) or len(payload) != 1:
            raise BinanceProtocolError("15-minute kline response must contain exactly one candle")
        candle = payload[0]
        if not isinstance(candle, list) or len(candle) < 7:
            raise BinanceProtocolError("15-minute kline response has an invalid candle row")
        if (
            _integer(candle[0], "openTime") != slot_start_ms
            or _integer(candle[6], "closeTime") != close_time_ms
        ):
            raise BinanceProtocolError("15-minute kline response does not match the requested slot")
        return FifteenMinuteCloseSnapshot(
            symbol=symbol,
            close_price=_decimal(candle[4], "close"),
        )

    async def get_position_snapshot(self) -> tuple[ExchangePosition, ...]:
        """Fetch position quantities for all USDⓈ-M symbols."""

        payload = await self._request_json(
            "GET",
            "/fapi/v3/positionRisk",
            signed=True,
        )
        if not isinstance(payload, list):
            raise BinanceProtocolError("positionRisk response must be a list")
        positions: list[ExchangePosition] = []
        for index, row in enumerate(payload):
            if not isinstance(row, dict):
                raise BinanceProtocolError(f"positionRisk row {index} must be an object")
            symbol = row.get("symbol")
            position_side = row.get("positionSide")
            if not isinstance(symbol, str) or not symbol:
                raise BinanceProtocolError(f"positionRisk row {index} has no symbol")
            if not isinstance(position_side, str) or not position_side:
                raise BinanceProtocolError(f"positionRisk row {index} has no positionSide")
            positions.append(
                ExchangePosition(
                    symbol=symbol,
                    position_side=position_side,
                    position_amt=_decimal(row.get("positionAmt"), "positionAmt"),
                )
            )
        return tuple(positions)

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        params = {
            "symbol": request.symbol,
            "side": request.side,
            "type": "MARKET",
            "quantity": _format_decimal(request.quantity),
            "newClientOrderId": request.client_order_id,
            "reduceOnly": str(request.side == "SELL").lower(),
            "newOrderRespType": "RESULT",
        }
        try:
            payload = await self._request_json(
                "POST",
                "/fapi/v1/order",
                params=params,
                signed=True,
            )
            order = _parse_order_snapshot(payload)
        except BinanceApiError as exc:
            certainty = (
                SubmissionCertainty.UNKNOWN
                if exc.execution_unknown
                else SubmissionCertainty.REJECTED
            )
            return SubmissionResult(certainty, detail=str(exc))
        except Exception as exc:
            return SubmissionResult(
                SubmissionCertainty.UNKNOWN,
                detail=(
                    f"submit response could not be confirmed ({type(exc).__name__}); "
                    "lookup required"
                ),
            )

        if order.status is ExchangeOrderStatus.REJECTED:
            return SubmissionResult(
                SubmissionCertainty.REJECTED,
                detail="exchange returned a REJECTED order status",
            )
        return SubmissionResult(SubmissionCertainty.ACCEPTED, order=order)

    async def lookup_order(
        self,
        symbol: str,
        client_order_id: str,
    ) -> ExchangeOrderSnapshot | None:
        try:
            payload = await self._request_json(
                "GET",
                "/fapi/v1/order",
                params={"symbol": symbol, "origClientOrderId": client_order_id},
                signed=True,
            )
        except BinanceApiError as exc:
            if exc.code == -2013:
                return None
            raise
        if not isinstance(payload, dict):
            raise BinanceProtocolError("query order response must be an object")
        if payload.get("symbol") != symbol or payload.get("clientOrderId") != client_order_id:
            raise BinanceProtocolError("query order response does not match the requested order")
        return _parse_order_snapshot(payload)

    async def list_order_fills(
        self,
        symbol: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        order_id = order_snapshot.exchange_order_id
        payload = await self._request_json(
            "GET",
            "/fapi/v1/userTrades",
            params={"symbol": symbol, "orderId": order_id, "limit": "1000"},
            signed=True,
        )
        if not isinstance(payload, list):
            raise BinanceProtocolError("userTrades response must be a list")
        fills = tuple(
            _parse_trade_fill(row, symbol, order_side, order_id)
            for row in payload
        )
        total_qty = sum((fill.qty for fill in fills), ZERO)
        total_quote = sum((fill.price * fill.qty for fill in fills), ZERO)
        if (
            total_qty != order_snapshot.executed_qty
            or total_quote != order_snapshot.cumulative_quote
        ):
            raise BinanceProtocolError("userTrades do not reconcile with the queried order totals")
        return fills

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        signed: bool = False,
    ) -> Any:
        """Send a REST request and validate its JSON response.

        Idempotent reads retry transport failures, server errors, and rate limits
        three times with exponential backoff. Order submissions are never retried
        here: after a transport failure their exchange outcome must be looked up
        by client order ID before the system can decide what to do next.
        """

        request_params = dict(params or {})
        read_only = method == "GET"
        max_attempts = 4 if read_only else 1
        headers = {"Accept": "application/json"}
        if signed:
            headers["X-MBX-APIKEY"] = self._api_key

        for attempt in range(max_attempts):
            attempt_params = dict(request_params)
            if signed:
                attempt_params["timestamp"] = str(self._clock_ms())
                attempt_params["recvWindow"] = str(BINANCE_RECV_WINDOW_MS)
                encoded = urlencode(attempt_params)
                signature = hmac.new(
                    self._api_secret,
                    encoded.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                url = f"{BINANCE_USDM_FUTURES_URL}{path}?{encoded}&signature={signature}"
                attempt_params = None
            else:
                url = f"{BINANCE_USDM_FUTURES_URL}{path}"

            try:
                response = await self._client.request(
                    method,
                    url,
                    params=attempt_params,
                    headers=headers,
                )
            except httpx.TransportError:
                if attempt + 1 >= max_attempts:
                    raise
                await asyncio.sleep(1 << attempt)
                continue

            used_weight = response.headers.get("X-MBX-USED-WEIGHT-1M")
            if used_weight is not None:
                logger.debug(
                    "Binance request usage: path=%s status=%s used_weight_1m=%s order_count_1m=%s",
                    path,
                    response.status_code,
                    used_weight,
                    response.headers.get("X-MBX-ORDER-COUNT-1M"),
                )
            if response.status_code == 429:
                logger.warning(
                    "Binance rate limit response: path=%s used_weight_1m=%s order_count_1m=%s retry_after=%s",
                    path,
                    used_weight,
                    response.headers.get("X-MBX-ORDER-COUNT-1M"),
                    response.headers.get("Retry-After"),
                )

            retryable_response = response.status_code == 429 or response.status_code >= 500
            if read_only and retryable_response and attempt + 1 < max_attempts:
                delay_seconds = 1 << attempt
                retry_after = response.headers.get("Retry-After")
                if response.status_code == 429 and retry_after is not None:
                    delay_seconds = max(delay_seconds, float(retry_after))
                await asyncio.sleep(delay_seconds)
                continue
            break

        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceProtocolError("exchange returned a non-JSON response") from exc

        if not isinstance(payload, dict):
            if response.status_code >= 400:
                raise BinanceProtocolError("error response must be an object")
            return payload
        if "code" in payload:
            code = payload["code"]
            if isinstance(code, bool) or not isinstance(code, int):
                raise BinanceProtocolError("code is not an integer")
            if response.status_code >= 400 or code < 0:
                raise BinanceApiError(
                    response.status_code,
                    code,
                    _required_text(payload.get("msg"), "msg"),
                )
        elif response.status_code >= 400:
            raise BinanceProtocolError("error response is missing code")
        return payload


def _parse_instrument_rules(
    row: Mapping[str, Any],
    *,
    symbol: str,
    contract_type: str,
    status: str,
    quote_asset: str,
    margin_asset: str,
) -> InstrumentRules:
    filters = row.get("filters")
    if not isinstance(filters, list):
        raise BinanceProtocolError("exchangeInfo symbol is missing filters")
    by_type = {
        item.get("filterType"): item
        for item in filters
        if isinstance(item, dict) and isinstance(item.get("filterType"), str)
    }
    market_filter = by_type.get("MARKET_LOT_SIZE")
    if not isinstance(market_filter, dict):
        raise BinanceProtocolError(f"{symbol} has no MARKET_LOT_SIZE filter")

    min_qty = _decimal(market_filter.get("minQty"), "minQty")
    max_qty = _decimal(market_filter.get("maxQty"), "maxQty")
    step_size = _decimal(market_filter.get("stepSize"), "stepSize")
    notional_filter = by_type.get("MIN_NOTIONAL")
    if not isinstance(notional_filter, dict):
        raise BinanceProtocolError(f"{symbol} has no MIN_NOTIONAL filter")
    min_market_notional = _decimal(notional_filter.get("notional"), "notional")

    return InstrumentRules(
        symbol=symbol,
        contract_type=contract_type,
        status=status,
        quote_asset=quote_asset,
        margin_asset=margin_asset,
        market_quantity=MarketQuantityRules(
            min_qty=min_qty,
            max_qty=max_qty,
            step_size=step_size,
        ),
        min_market_notional=min_market_notional,
    )


def _parse_order_snapshot(payload: Any) -> ExchangeOrderSnapshot:
    if not isinstance(payload, dict):
        raise BinanceProtocolError("order response must be an object")
    raw_status = _required_text(payload.get("status"), "status")
    try:
        status = ExchangeOrderStatus(raw_status)
    except ValueError as exc:
        raise BinanceProtocolError(f"unsupported order status {raw_status!r}") from exc
    executed_qty = _decimal(payload.get("executedQty"), "executedQty")
    cumulative_quote = _decimal(payload.get("cumQuote"), "cumQuote")
    return ExchangeOrderSnapshot(
        status=status,
        executed_qty=executed_qty,
        cumulative_quote=cumulative_quote,
        exchange_order_id=str(_integer(payload.get("orderId"), "orderId")),
    )


def _parse_trade_fill(
    row: Mapping[str, Any],
    symbol: str,
    order_side: str,
    expected_order_id: str,
) -> TradeFillRecord:
    if not isinstance(row, dict):
        raise BinanceProtocolError("userTrades row must be an object")
    exchange_order_id = str(_integer(row.get("orderId"), "orderId"))
    if exchange_order_id != expected_order_id:
        raise BinanceProtocolError("userTrades returned a fill for a different order")
    side = _required_text(row.get("side"), "side")
    if side != order_side:
        raise BinanceProtocolError("userTrades fill side does not match its order")
    trade_symbol = row.get("symbol")
    if trade_symbol != symbol:
        raise BinanceProtocolError("userTrades returned a fill for a different symbol")
    commission_asset = _required_text(row.get("commissionAsset"), "commissionAsset")
    return TradeFillRecord(
        symbol=symbol,
        exchange_trade_id=str(_integer(row.get("id"), "id")),
        side=side,
        price=_decimal(row.get("price"), "price"),
        qty=_decimal(row.get("qty"), "qty"),
        realized_pnl=_decimal(row.get("realizedPnl"), "realizedPnl"),
        commission=_decimal(row.get("commission"), "commission"),
        commission_asset=commission_asset,
        traded_at_ms=_integer(row.get("time"), "time"),
    )


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise BinanceProtocolError(f"{field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise BinanceProtocolError(f"{field} is not a decimal value") from exc
    if not result.is_finite():
        raise BinanceProtocolError(f"{field} is not finite")
    return result


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BinanceProtocolError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BinanceProtocolError(f"{field} is not an integer")
    if value < 0:
        raise BinanceProtocolError(f"{field} must be non-negative")
    return value


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")
