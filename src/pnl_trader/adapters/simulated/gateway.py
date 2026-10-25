"""A scripted, in-memory ExchangeGatewayPort adapter; it never performs I/O."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from ...application.execution_models import (
    ExchangeOrderSnapshot,
    SubmissionCertainty,
    SubmissionResult,
    MarketOrderRequest,
)
from ...application.market_models import (
    FifteenMinuteCloseSnapshot,
    InstrumentRules,
)
from ...domain.models import TradeFillRecord, ZERO


@dataclass(frozen=True, slots=True)
class SimulatedOrderScenario:
    submission: SubmissionResult
    lookup_snapshot: ExchangeOrderSnapshot | None = None
    fills: tuple[TradeFillRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.submission.certainty is SubmissionCertainty.REJECTED:
            if self.lookup_snapshot is not None or self.fills:
                raise ValueError("a rejected scenario cannot contain an order or fills")
        if len({fill.exchange_trade_id for fill in self.fills}) != len(self.fills):
            raise ValueError("a simulated scenario cannot repeat an exchange trade ID")
        if len({fill.client_order_id for fill in self.fills}) > 1:
            raise ValueError("simulated fills must belong to one client order ID")
        if len({fill.symbol for fill in self.fills}) > 1:
            raise ValueError("simulated fills must belong to one symbol")

        final_snapshot = self.lookup_snapshot or self.submission.order
        if final_snapshot is not None and final_snapshot.executed_qty > ZERO:
            if not self.fills:
                raise ValueError("a simulated executed order requires fill records")
            total_qty = sum((fill.qty for fill in self.fills), ZERO)
            total_quote = sum((fill.price * fill.qty for fill in self.fills), ZERO)
            if total_qty != final_snapshot.executed_qty:
                raise ValueError("simulated fills do not match the final executed quantity")
            if total_quote != final_snapshot.cumulative_quote:
                raise ValueError("simulated fills do not match the final cumulative quote")


class ScriptedExchangeGateway:
    """Replay explicitly prepared exchange outcomes through the production port.

    Each scenario is keyed by ``client_order_id``. Unknown submission outcomes
    can carry a later lookup snapshot to model a response lost after acceptance.
    Orders not found in the supplied scenarios remain unknown; this adapter never
    invents a rejection or turns absence into proof of no execution.
    """

    def __init__(
        self,
        scenarios: Mapping[str, SimulatedOrderScenario],
        *,
        instrument_rules: Mapping[str, InstrumentRules] | None = None,
        close_prices: Mapping[str, Decimal] | None = None,
    ) -> None:
        self._scenarios = dict(scenarios)
        self._instrument_rules = dict(instrument_rules or {})
        self._close_prices = dict(close_prices or {})
        self._submitted: dict[str, tuple[MarketOrderRequest, SimulatedOrderScenario]] = {}
        self._lock = asyncio.Lock()

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules:
        try:
            return self._instrument_rules[symbol]
        except KeyError as exc:
            raise LookupError(f"no scripted instrument rules for {symbol}") from exc

    async def get_fifteen_minute_close(
        self,
        symbol: str,
        slot_start_ms: int,
    ) -> FifteenMinuteCloseSnapshot:
        try:
            close_price = self._close_prices[symbol]
        except KeyError as exc:
            raise LookupError(f"no scripted close price for {symbol}") from exc
        return FifteenMinuteCloseSnapshot(
            symbol=symbol,
            slot_start_ms=slot_start_ms,
            close_price=close_price,
            close_time_ms=slot_start_ms + 899_999,
        )

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        async with self._lock:
            prior = self._submitted.get(request.client_order_id)
            if prior is not None:
                if prior[0] != request:
                    raise ValueError("client_order_id was reused for a different market order")
                return prior[1].submission

            scenario = self._scenarios.get(request.client_order_id)
            if scenario is None:
                return SubmissionResult(
                    SubmissionCertainty.UNKNOWN,
                    detail="no scripted response; treat the submit outcome as unknown",
                )
            self._validate_scenario_request(request, scenario)
            self._submitted[request.client_order_id] = (request, scenario)
            return scenario.submission

    async def lookup_order(
        self,
        symbol: str,
        client_order_id: str,
    ) -> ExchangeOrderSnapshot | None:
        async with self._lock:
            submitted = self._submitted.get(client_order_id)
            if submitted is None:
                return None
            request, scenario = submitted
            if request.symbol != symbol:
                raise ValueError("lookup symbol does not match the submitted order")
            return scenario.lookup_snapshot or scenario.submission.order

    async def list_order_fills(
        self,
        symbol: str,
        client_order_id: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        async with self._lock:
            submitted = self._submitted.get(client_order_id)
            if submitted is None:
                return ()
            request, scenario = submitted
            if request.symbol != symbol:
                raise ValueError("fill query symbol does not match the submitted order")
            if request.side != order_side:
                raise ValueError("fill query side does not match the submitted order")
            return scenario.fills

    @staticmethod
    def _validate_scenario_request(
        request: MarketOrderRequest,
        scenario: SimulatedOrderScenario,
    ) -> None:
        order = scenario.lookup_snapshot or scenario.submission.order
        for fill in scenario.fills:
            if fill.symbol != request.symbol or fill.client_order_id != request.client_order_id:
                raise ValueError("simulated fills do not match the submitted order")
            if fill.side != request.side:
                raise ValueError("simulated fill side does not match the submitted order")
        if order is not None and order.executed_qty > request.quantity:
            raise ValueError("simulated execution exceeds the submitted quantity")
