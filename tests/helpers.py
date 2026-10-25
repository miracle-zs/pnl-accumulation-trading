from decimal import Decimal

from pnl_trader.adapters.sqlite import SQLiteStateStore
from pnl_trader.application.execution_models import IntentStatus, OrderIntent
from pnl_trader.domain import Action, ExecutionReceipt, SymbolState, TradeFillRecord


def seed_buy_execution(
    store: SQLiteStateStore,
    state: SymbolState,
    *,
    action: Action,
    quantity: Decimal,
    price: Decimal,
    intent_id: str,
    timestamp_ms: int,
) -> SymbolState:
    intent = OrderIntent(
        intent_id=intent_id,
        symbol=state.symbol,
        action=action,
        created_at_ms=timestamp_ms,
    )
    store.create_order_intent(intent)
    store.create_order_attempt(intent_id, requested_qty=quantity)
    fill = TradeFillRecord(
        symbol=state.symbol,
        exchange_trade_id=f"trade-{intent_id}",
        side="BUY",
        price=price,
        qty=quantity,
        realized_pnl=Decimal("0"),
        commission=Decimal("0"),
        commission_asset="USDT",
        traded_at_ms=timestamp_ms,
    )
    store.commit_confirmed_execution(
        intent_id=intent_id,
        receipt=ExecutionReceipt(action, quantity, quantity * price),
        fills=(fill,),
        expected_intent_status=IntentStatus.PENDING,
        terminal_status=IntentStatus.FILLED,
        updated_at_ms=timestamp_ms,
    )
    return store.load_symbol_state(state.symbol)
