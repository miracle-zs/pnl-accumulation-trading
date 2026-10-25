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
from decimal import Decimal
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
    ExchangeInfoSnapshot,
    ExchangeInstrument,
    FifteenMinuteCloseSnapshot,
    InstrumentRules,
    MarketQuantityRules,
)
from ...application.order_reconciliation_models import (
    ExchangeOpenOrder,
    ExchangeOpenOrdersSnapshot,
)
from ...application.position_models import ExchangePosition, ExchangePositionSnapshot
from ...domain.models import TradeFillRecord, ZERO


BINANCE_USDM_FUTURES_URL = "https://fapi.binance.com"
logger = logging.getLogger(__name__)


class BinanceGatewayError(RuntimeError):
    """Base error for malformed or unsuccessful exchange responses."""


class BinanceApiError(BinanceGatewayError):
    def __init__(self, status_code: int, code: int | None, message: str) -> None:
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


class BinanceProtocolError(BinanceGatewayError):
    """The exchange response could not be interpreted safely."""


class BinanceFuturesGateway:
    """ExchangeGatewayPort implementation for one-way USDⓈ-M futures."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        api_secret: str,
        base_url: str = BINANCE_USDM_FUTURES_URL,
        recv_window_ms: int = 5_000,
        rules_cache_ttl_ms: int = 86_400_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret must not be empty")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an absolute HTTP URL")
        if (
            recv_window_ms <= 0
            or rules_cache_ttl_ms < 0
        ):
            raise ValueError("recv_window_ms must be positive and rules cache TTL non-negative")
        self._client = client
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        self._recv_window_ms = recv_window_ms
        self._rules_cache_ttl_ms = rules_cache_ttl_ms
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._exchange_info_snapshot: ExchangeInfoSnapshot | None = None
        self._exchange_info_loaded_at_ms: int | None = None
        self._exchange_info_lock = asyncio.Lock()

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules:
        if not symbol:
            raise ValueError("symbol must not be empty")
        snapshot = await self.get_exchange_info_snapshot()
        instrument = next((item for item in snapshot.instruments if item.symbol == symbol), None)
        if instrument is None:
            raise LookupError(f"exchangeInfo does not contain {symbol}")
        if instrument.rules is None:
            raise LookupError(f"exchangeInfo does not contain usable market rules for {symbol}")
        return instrument.rules

    async def get_exchange_info_snapshot(self) -> ExchangeInfoSnapshot:
        """Fetch the complete symbol catalog, preserving rows with unusable order filters."""

        async with self._exchange_info_lock:
            now_ms = self._clock_ms()
            fresh = (
                self._exchange_info_snapshot is not None
                and self._exchange_info_loaded_at_ms is not None
                and now_ms - self._exchange_info_loaded_at_ms < self._rules_cache_ttl_ms
            )
            if fresh:
                return self._exchange_info_snapshot

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
                    rules = _parse_instrument_rules(row)
                except (BinanceProtocolError, TypeError, ValueError):
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

            sampled_at_ms = self._clock_ms()
            ordered = sorted(instruments, key=lambda item: item.symbol)
            digest_parts = [str(sampled_at_ms)]
            for item in ordered:
                digest_parts.extend(
                    (
                        item.symbol,
                        item.base_asset,
                        item.quote_asset,
                        item.margin_asset,
                        item.contract_type,
                        item.status,
                        "rules" if item.rules is not None else "no-rules",
                    )
                )
                if item.rules is not None:
                    digest_parts.extend(
                        (
                            str(item.rules.market_quantity.min_qty),
                            str(item.rules.market_quantity.max_qty),
                            str(item.rules.market_quantity.step_size),
                            str(item.rules.min_market_notional),
                            str(item.rules.max_market_notional),
                        )
                    )
            snapshot = ExchangeInfoSnapshot(
                snapshot_id=hashlib.sha256("\0".join(digest_parts).encode("utf-8")).hexdigest(),
                sampled_at_ms=sampled_at_ms,
                instruments=tuple(ordered),
            )
            self._exchange_info_snapshot = snapshot
            self._exchange_info_loaded_at_ms = sampled_at_ms
            return snapshot

    async def get_fifteen_minute_close(
        self,
        symbol: str,
        slot_start_ms: int,
    ) -> FifteenMinuteCloseSnapshot:
        if not symbol:
            raise ValueError("symbol must not be empty")
        if (
            isinstance(slot_start_ms, bool)
            or not isinstance(slot_start_ms, int)
            or slot_start_ms < 0
            or slot_start_ms % 900_000 != 0
        ):
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")

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
            slot_start_ms=slot_start_ms,
            close_price=_decimal(candle[4], "close"),
            close_time_ms=close_time_ms,
        )

    async def get_position_snapshot(self, account_id: str) -> ExchangePositionSnapshot:
        """Fetch and strictly normalize the complete USDⓈ-M position response."""

        if not account_id:
            raise ValueError("account_id must not be empty")
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
                    entry_price=_decimal(row.get("entryPrice"), "entryPrice"),
                    update_time_ms=_integer(row.get("updateTime"), "updateTime"),
                )
            )
        return ExchangePositionSnapshot(
            account_id=account_id,
            sampled_at_ms=self._clock_ms(),
            positions=tuple(positions),
        )

    async def get_open_orders_snapshot(
        self,
        account_id: str,
    ) -> ExchangeOpenOrdersSnapshot:
        """Fetch regular and algorithmic open orders across all symbols."""

        if not account_id:
            raise ValueError("account_id must not be empty")
        regular_payload, algo_payload = await asyncio.gather(
            self._request_json("GET", "/fapi/v1/openOrders", signed=True),
            self._request_json("GET", "/fapi/v1/openAlgoOrders", signed=True),
        )
        if not isinstance(regular_payload, list):
            raise BinanceProtocolError("openOrders response must be a list")
        if not isinstance(algo_payload, list):
            raise BinanceProtocolError("openAlgoOrders response must be a list")

        orders = [
            _parse_standard_open_order(row, index)
            for index, row in enumerate(regular_payload)
        ]
        orders.extend(
            _parse_algorithmic_open_order(row, index)
            for index, row in enumerate(algo_payload)
        )
        return ExchangeOpenOrdersSnapshot(
            account_id=account_id,
            sampled_at_ms=self._clock_ms(),
            orders=tuple(orders),
        )

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        params = {
            "symbol": request.symbol,
            "side": request.side,
            "type": "MARKET",
            "quantity": _format_decimal(request.quantity),
            "newClientOrderId": request.client_order_id,
            "reduceOnly": str(request.reduce_only).lower(),
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
            payload = await self._lookup_order_record(symbol, client_order_id)
        except BinanceApiError as exc:
            if exc.code == -2013:
                return None
            raise
        return _parse_order_snapshot(payload)

    async def list_order_fills(
        self,
        symbol: str,
        client_order_id: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        if order_side not in {"BUY", "SELL"}:
            raise ValueError("order_side must be BUY or SELL")
        snapshot = order_snapshot
        order_id = order_snapshot.exchange_order_id
        if order_id is None:
            # Older or incomplete exchange responses may omit orderId. Keep the
            # lookup fallback for that case; complete snapshots need no second read.
            order = await self._lookup_order_record(symbol, client_order_id)
            order_id = order.get("orderId")
            queried_side = order.get("side")
            if order_id is None or queried_side not in {"BUY", "SELL"}:
                raise BinanceProtocolError("queried order is missing orderId or side")
            if queried_side != order_side:
                raise BinanceProtocolError("queried order side does not match the intent")
            snapshot = _parse_order_snapshot(order)
        payload = await self._request_json(
            "GET",
            "/fapi/v1/userTrades",
            params={"symbol": symbol, "orderId": str(order_id), "limit": "1000"},
            signed=True,
        )
        if not isinstance(payload, list):
            raise BinanceProtocolError("userTrades response must be a list")
        order_id_text = str(order_id)
        for row in payload:
            if not isinstance(row, dict) or str(row.get("orderId")) != order_id_text:
                raise BinanceProtocolError("userTrades returned a fill for a different order")
        fills = tuple(
            _parse_trade_fill(row, symbol, client_order_id, order_side)
            for row in payload
        )
        total_qty = sum((fill.qty for fill in fills), ZERO)
        total_quote = sum((fill.price * fill.qty for fill in fills), ZERO)
        if total_qty != snapshot.executed_qty or total_quote != snapshot.cumulative_quote:
            raise BinanceProtocolError("userTrades do not reconcile with the queried order totals")
        return fills



    async def _lookup_order_record(self, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        if not symbol or not client_order_id:
            raise ValueError("symbol and client_order_id must not be empty")
        payload = await self._request_json(
            "GET",
            "/fapi/v1/order",
            params={"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
        )
        if not isinstance(payload, dict):
            raise BinanceProtocolError("query order response must be an object")
        if payload.get("symbol") != symbol or payload.get("clientOrderId") != client_order_id:
            raise BinanceProtocolError("query order response does not match the requested order")
        return payload

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
        read_only = method.upper() in {"GET", "HEAD"}
        max_attempts = 4 if read_only else 1
        headers = {"Accept": "application/json"}
        if signed:
            headers["X-MBX-APIKEY"] = self._api_key

        for attempt in range(max_attempts):
            attempt_params = dict(request_params)
            if signed:
                attempt_params["timestamp"] = str(self._clock_ms())
                attempt_params["recvWindow"] = str(self._recv_window_ms)
                encoded = urlencode(attempt_params)
                signature = hmac.new(
                    self._api_secret,
                    encoded.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                url = f"{self._base_url}{path}?{encoded}&signature={signature}"
                attempt_params = None
            else:
                url = f"{self._base_url}{path}"

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
                    try:
                        delay_seconds = max(delay_seconds, float(retry_after))
                    except ValueError:
                        pass
                await asyncio.sleep(delay_seconds)
                continue
            break

        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceProtocolError("exchange returned a non-JSON response") from exc

        if response.status_code >= 400:
            code = _optional_integer(payload.get("code")) if isinstance(payload, dict) else None
            message = (
                str(payload.get("msg", "request failed"))
                if isinstance(payload, dict)
                else "request failed"
            )
            raise BinanceApiError(response.status_code, code, message)
        if isinstance(payload, dict):
            code = _optional_integer(payload.get("code"))
            if code is not None and code < 0:
                raise BinanceApiError(
                    response.status_code,
                    code,
                    str(payload.get("msg", "request failed")),
                )
        return payload


def _parse_instrument_rules(row: Mapping[str, Any]) -> InstrumentRules:
    filters = row.get("filters")
    if not isinstance(filters, list):
        raise BinanceProtocolError("exchangeInfo symbol is missing filters")
    by_type = {
        item.get("filterType"): item
        for item in filters
        if isinstance(item, dict) and isinstance(item.get("filterType"), str)
    }
    market_filter = by_type.get("MARKET_LOT_SIZE")
    lot_filter = by_type.get("LOT_SIZE")
    if _usable_quantity_filter(market_filter):
        selected = market_filter
        step_origin = ZERO
    elif _usable_quantity_filter(lot_filter):
        selected = lot_filter
        step_origin = _decimal(selected.get("minQty"), "minQty")
    else:
        raise BinanceProtocolError(f"{row.get('symbol')} has no usable market quantity filter")

    min_qty = _decimal(selected.get("minQty"), "minQty")
    max_qty = _decimal(selected.get("maxQty"), "maxQty")
    step_size = _decimal(selected.get("stepSize"), "stepSize")
    minimums: list[Decimal] = []
    maximums: list[Decimal] = []
    for item in filters:
        if not isinstance(item, dict):
            continue
        filter_type = item.get("filterType")
        if filter_type == "MIN_NOTIONAL" and _as_bool(item.get("applyToMarket", True)):
            minimums.append(_decimal(item.get("notional"), "notional"))
        elif filter_type == "NOTIONAL":
            if _as_bool(item.get("applyMinToMarket", True)) and item.get("minNotional") is not None:
                minimums.append(_decimal(item.get("minNotional"), "minNotional"))
            if (
                _as_bool(item.get("applyMaxToMarket", False))
                and item.get("maxNotional") is not None
            ):
                maximums.append(_decimal(item.get("maxNotional"), "maxNotional"))

    return InstrumentRules(
        symbol=str(row.get("symbol", "")),
        contract_type=str(row.get("contractType", "")),
        status=str(row.get("status", "")),
        quote_asset=str(row.get("quoteAsset", "")),
        margin_asset=str(row.get("marginAsset", "")),
        market_quantity=MarketQuantityRules(
            min_qty=min_qty,
            max_qty=max_qty,
            step_size=step_size,
            step_origin=step_origin,
        ),
        min_market_notional=max(minimums) if minimums else None,
        max_market_notional=min(maximums) if maximums else None,
    )


def _usable_quantity_filter(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    try:
        minimum = _decimal(item.get("minQty"), "minQty")
        maximum = _decimal(item.get("maxQty"), "maxQty")
        step = _decimal(item.get("stepSize"), "stepSize")
    except BinanceProtocolError:
        return False
    return minimum > ZERO and maximum >= minimum and step > ZERO


def _parse_order_snapshot(payload: Any) -> ExchangeOrderSnapshot:
    if not isinstance(payload, dict):
        raise BinanceProtocolError("order response must be an object")
    raw_status = payload.get("status")
    try:
        status = ExchangeOrderStatus(str(raw_status))
    except ValueError as exc:
        raise BinanceProtocolError(f"unsupported order status {raw_status!r}") from exc
    executed_qty = _decimal(payload.get("executedQty", "0"), "executedQty")
    quote_value = payload.get("cumQuote", payload.get("cummulativeQuoteQty", "0"))
    cumulative_quote = _decimal(quote_value, "cumQuote")
    order_id = payload.get("orderId")
    return ExchangeOrderSnapshot(
        status=status,
        executed_qty=executed_qty,
        cumulative_quote=cumulative_quote,
        exchange_order_id=None if order_id is None else str(order_id),
    )


def _parse_trade_fill(
    row: Mapping[str, Any],
    symbol: str,
    client_order_id: str,
    order_side: str,
) -> TradeFillRecord:
    side = row.get("side", order_side)
    if side not in {"BUY", "SELL"}:
        buyer = row.get("buyer")
        if not isinstance(buyer, bool):
            raise BinanceProtocolError("userTrades fill is missing side/buyer")
        side = "BUY" if buyer else "SELL"
    if side != order_side:
        raise BinanceProtocolError("userTrades fill side does not match its order")
    trade_symbol = row.get("symbol")
    if trade_symbol != symbol:
        raise BinanceProtocolError("userTrades returned a fill for a different symbol")
    commission_asset = row.get("commissionAsset")
    if not commission_asset:
        raise BinanceProtocolError("userTrades fill is missing commissionAsset")
    return TradeFillRecord(
        symbol=symbol,
        exchange_trade_id=str(row.get("id", "")),
        client_order_id=client_order_id,
        side=side,
        price=_decimal(row.get("price"), "price"),
        qty=_decimal(row.get("qty"), "qty"),
        realized_pnl=_decimal(row.get("realizedPnl", "0"), "realizedPnl"),
        commission=_decimal(row.get("commission", "0"), "commission"),
        commission_asset=str(commission_asset),
        traded_at_ms=_integer(row.get("time"), "time"),
        exchange_order_id=(
            None
            if row.get("orderId") is None
            else str(_strict_integer(row.get("orderId"), "orderId"))
        ),
        position_side=_optional_text(row.get("positionSide"), "positionSide"),
        margin_asset=_optional_text(row.get("marginAsset"), "marginAsset"),
    )






def _optional_text(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BinanceProtocolError(f"{field} must be a string when present")
    return value


def _strict_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BinanceProtocolError(f"{field} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value)
        except ValueError as exc:
            raise BinanceProtocolError(f"{field} must be an integer") from exc
    else:
        raise BinanceProtocolError(f"{field} must be an integer")
    if result < 0:
        raise BinanceProtocolError(f"{field} must be non-negative")
    return result


def _parse_standard_open_order(row: Any, index: int) -> ExchangeOpenOrder:
    if not isinstance(row, dict):
        raise BinanceProtocolError(f"openOrders row {index} must be an object")
    try:
        exchange_order_id = _integer(row.get("orderId"), "orderId")
        if exchange_order_id <= 0:
            raise BinanceProtocolError("orderId must be positive")
        return ExchangeOpenOrder(
            symbol=_required_text(row.get("symbol"), "symbol"),
            exchange_order_id=str(exchange_order_id),
            client_order_id=_required_text(row.get("clientOrderId"), "clientOrderId"),
            side=_required_text(row.get("side"), "side"),
            position_side=_required_text(row.get("positionSide"), "positionSide"),
            order_type=_required_text(row.get("type"), "type"),
            status=_required_text(row.get("status"), "status"),
            reduce_only=_required_bool(row.get("reduceOnly"), "reduceOnly"),
            updated_at_ms=_integer(row.get("updateTime"), "updateTime"),
            original_qty=_decimal(row.get("origQty"), "origQty"),
            executed_qty=_decimal(row.get("executedQty"), "executedQty"),
        )
    except (BinanceProtocolError, TypeError, ValueError) as exc:
        raise BinanceProtocolError(f"openOrders row {index} is invalid: {exc}") from exc


def _parse_algorithmic_open_order(row: Any, index: int) -> ExchangeOpenOrder:
    if not isinstance(row, dict):
        raise BinanceProtocolError(f"openAlgoOrders row {index} must be an object")
    try:
        algo_id = _integer(row.get("algoId"), "algoId")
        if algo_id <= 0:
            raise BinanceProtocolError("algoId must be positive")
        return ExchangeOpenOrder(
            symbol=_required_text(row.get("symbol"), "symbol"),
            exchange_order_id=str(algo_id),
            client_order_id=_required_text(row.get("clientAlgoId"), "clientAlgoId"),
            side=_required_text(row.get("side"), "side"),
            position_side=_required_text(row.get("positionSide"), "positionSide"),
            order_type=_required_text(row.get("orderType"), "orderType"),
            status=_required_text(row.get("algoStatus"), "algoStatus"),
            reduce_only=_required_bool(row.get("reduceOnly"), "reduceOnly"),
            updated_at_ms=_integer(row.get("updateTime"), "updateTime"),
            algorithmic=True,
        )
    except (BinanceProtocolError, TypeError, ValueError) as exc:
        raise BinanceProtocolError(f"openAlgoOrders row {index} is invalid: {exc}") from exc


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise BinanceProtocolError(f"{field} is not a decimal value") from exc
    if not result.is_finite():
        raise BinanceProtocolError(f"{field} is not finite")
    return result


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BinanceProtocolError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BinanceProtocolError(f"{field} is not an integer") from exc


def _optional_integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _required_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise BinanceProtocolError(f"{field} is not a boolean value")


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")
